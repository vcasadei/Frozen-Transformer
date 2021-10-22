# coding: utf-8

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from transformers import BertConfig, BertModel, MBartConfig, MBartModel

from signjoey.helpers import freeze_params
from signjoey.transformer_layers import TransformerEncoderLayer, PositionalEncoding, BERTIdentity, mBARTIdentity


# pylint: disable=abstract-method


class Encoder(nn.Module):
    """
    Base encoder class
    """

    @property
    def output_size(self):
        """
        Return the output size

        :return:
        """
        return self._output_size


class RecurrentEncoder(Encoder):
    """Encodes a sequence of word embeddings"""

    # pylint: disable=unused-argument
    def __init__(
            self,
            rnn_type: str = "gru",
            hidden_size: int = 1,
            emb_size: int = 1,
            num_layers: int = 1,
            dropout: float = 0.0,
            emb_dropout: float = 0.0,
            bidirectional: bool = True,
            freeze: bool = False,
            **kwargs
    ) -> None:
        """
        Create a new recurrent encoder.

        :param rnn_type: RNN type: `gru` or `lstm`.
        :param hidden_size: Size of each RNN.
        :param emb_size: Size of the word embeddings.
        :param num_layers: Number of encoder RNN layers.
        :param dropout:  Is applied between RNN layers.
        :param emb_dropout: Is applied to the RNN input (word embeddings).
        :param bidirectional: Use a bi-directional RNN.
        :param freeze: freeze the parameters of the encoder during training
        :param kwargs:
        """

        super(RecurrentEncoder, self).__init__()

        self.emb_dropout = torch.nn.Dropout(p=emb_dropout, inplace=False)
        self.type = rnn_type
        self.emb_size = emb_size

        rnn = nn.GRU if rnn_type == "gru" else nn.LSTM

        self.rnn = rnn(
            emb_size,
            hidden_size,
            num_layers,
            batch_first=True,
            bidirectional=bidirectional,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        self._output_size = 2 * hidden_size if bidirectional else hidden_size

        if freeze:
            freeze_params(self)

    # pylint: disable=invalid-name, unused-argument
    def _check_shapes_input_forward(
            self, embed_src: Tensor, src_length: Tensor, mask: Tensor
    ) -> None:
        """
        Make sure the shape of the inputs to `self.forward` are correct.
        Same input semantics as `self.forward`.

        :param embed_src: embedded source tokens
        :param src_length: source length
        :param mask: source mask
        """
        assert embed_src.shape[0] == src_length.shape[0]
        assert embed_src.shape[2] == self.emb_size
        # assert mask.shape == embed_src.shape
        assert len(src_length.shape) == 1

    # pylint: disable=arguments-differ
    def forward(
            self, embed_src: Tensor, src_length: Tensor, mask: Tensor
    ) -> (Tensor, Tensor):
        """
        Applies a bidirectional RNN to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param mask: indicates padding areas (zeros where padding), shape
            (batch_size, src_len, embed_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        self._check_shapes_input_forward(
            embed_src=embed_src, src_length=src_length, mask=mask
        )

        # apply dropout to the rnn input
        embed_src = self.emb_dropout(embed_src)

        packed = pack_padded_sequence(embed_src, src_length, batch_first=True)
        output, hidden = self.rnn(packed)

        # pylint: disable=unused-variable
        if isinstance(hidden, tuple):
            hidden, memory_cell = hidden

        output, _ = pad_packed_sequence(output, batch_first=True)
        # hidden: dir*layers x batch x hidden
        # output: batch x max_length x directions*hidden
        batch_size = hidden.size()[1]
        # separate final hidden states by layer and direction
        hidden_layerwise = hidden.view(
            self.rnn.num_layers,
            2 if self.rnn.bidirectional else 1,
            batch_size,
            self.rnn.hidden_size,
        )
        # final_layers: layers x directions x batch x hidden

        # concatenate the final states of the last layer for each directions
        # thanks to pack_padded_sequence final states don't include padding
        fwd_hidden_last = hidden_layerwise[-1:, 0]
        bwd_hidden_last = hidden_layerwise[-1:, 1]

        # only feed the final state of the top-most layer to the decoder
        # pylint: disable=no-member
        hidden_concat = torch.cat([fwd_hidden_last, bwd_hidden_last], dim=2).squeeze(0)
        # final: batch x directions*hidden
        return output, hidden_concat

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.rnn)


class TransformerEncoder(Encoder):
    """
    Transformer Encoder
    """

    # pylint: disable=unused-argument
    def __init__(
            self,
            hidden_size: int = 512,
            ff_size: int = 2048,
            num_layers: int = 8,
            num_heads: int = 4,
            dropout: float = 0.1,
            emb_dropout: float = 0.1,
            freeze: bool = False,
            **kwargs
    ):
        """
        Initializes the Transformer.
        :param hidden_size: hidden size and size of embeddings
        :param ff_size: position-wise feed-forward layer size.
          (Typically this is 2*hidden_size.)
        :param num_layers: number of layers
        :param num_heads: number of heads for multi-headed attention
        :param dropout: dropout probability for Transformer layers
        :param emb_dropout: Is applied to the input (word embeddings).
        :param freeze: freeze the parameters of the encoder during training
        :param kwargs:
        """
        super(TransformerEncoder, self).__init__()

        # build all (num_layers) layers
        self.layers = nn.ModuleList(
            [
                TransformerEncoderLayer(
                    size=hidden_size,
                    ff_size=ff_size,
                    num_heads=num_heads,
                    dropout=dropout,
                )
                for _ in range(num_layers)
            ]
        )

        self.layer_norm = nn.LayerNorm(hidden_size, eps=1e-6)
        self.pe = PositionalEncoding(hidden_size)
        self.emb_dropout = nn.Dropout(p=emb_dropout)

        self._output_size = hidden_size

        if freeze:
            freeze_params(self)

    # pylint: disable=arguments-differ
    def forward(
            self, embed_src: Tensor, src_length: Tensor, mask: Tensor
    ) -> (Tensor, Tensor):
        """
        Pass the input (and mask) through each layer in turn.
        Applies a Transformer encoder to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param mask: indicates padding areas (zeros where padding), shape
            (batch_size, src_len, embed_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        x = self.pe(embed_src)  # add position encoding to word embeddings
        x = self.emb_dropout(x)

        for layer in self.layers:
            x = layer(x, mask)
        return self.layer_norm(x), None

    def __repr__(self):
        return "%s(num_layers=%r, num_heads=%r)" % (
            self.__class__.__name__,
            len(self.layers),
            self.layers[0].src_src_att.num_heads,
        )


class BERTEncoder(Encoder):
    """
    Transformer Encoder
    """

    # pylint: disable=unused-argument
    def __init__(
            self,
            hidden_size: int = 768,
            num_layers: int = 3,
            emb_dropout: float = 0.1,
            freeze: bool = False,
            freeze_pt: str = None,
            pretrained_name: str = 'bert-base-uncased',
            input_layer_init: str = None,
            **kwargs
    ):
        """
        Initializes the Transformer.
        :param hidden_size: hidden size and size of embeddings
        :param num_layers: number of layers to keep from BERT
        :param dropout: dropout probability for Transformer layers
        :param emb_dropout: Is applied to the input (word embeddings).
        :param freeze: freeze the parameters of the encoder during training
        :param pretrained_name: the name to pass to the Huggingface hub for the BERT model weights
        :param kwargs:
        """
        super(BERTEncoder, self).__init__()

        self.config = BertConfig.from_pretrained(pretrained_name)
        self.bert_model = BertModel.from_pretrained(pretrained_name)
        self.encoder = self.bert_model.encoder
        # Make sure our configuration file is compatible with the pretrained model
        assert self.encoder.config.hidden_size == hidden_size

        # Only keep given number of layers
        orig_nr_layers = len(self.encoder.layer)
        for i in range(orig_nr_layers - 1, num_layers - 1, -1):
            self.encoder.layer[i] = BERTIdentity()
        self.num_layers = num_layers

        # Freeze if needed. We only freeze the attention and intermediate layers, but keep the
        #  linear transformations in the "BertOutput" layer.
        print(f'Freezing pre-trained transformer: {freeze_pt}')
        print(f'Freezing entire encoder: {freeze}')
        # Default behavior, also for freeze_pt == 'layer_norm_only', is to freeze everything except layer norm
        for name, p in self.encoder.named_parameters():
            name = name.lower()
            if 'layernorm' not in name:
                p.requires_grad = False
        if freeze_pt == 'freeze_ff' or freeze_pt == 'finetune_ff':
            for name, p in self.encoder.named_parameters():
                name = name.lower()
                if 'output' in name and 'attention' not in name:
                    p.requires_grad = True
        if freeze_pt == 'finetune_ff':
            for name, p in self.encoder.named_parameters():
                name = name.lower()
                if 'intermediate' in name:
                    p.requires_grad = True

        # Add a linear transformation to our embeddings before passing them to BERT.
        self.input_layer = nn.Linear(hidden_size, hidden_size, bias=False)
        if input_layer_init == 'orthogonal':
            torch.nn.init.orthogonal_(self.input_layer.weight)

        self.layer_norm = nn.LayerNorm(hidden_size, eps=1e-6)
        self.pe = PositionalEncoding(hidden_size)
        self.emb_dropout = nn.Dropout(p=emb_dropout)

        self._output_size = hidden_size

    # pylint: disable=arguments-differ
    def forward(
            self, embed_src: Tensor, src_length: Tensor, mask: Tensor
    ) -> (Tensor, Tensor):
        """
        Pass the input (and mask) through each layer in turn.
        Applies a Transformer encoder to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param mask: indicates padding areas (zeros where padding), shape
            (batch_size, src_len, embed_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        x = self.pe(embed_src)  # add position encoding to word embeddings
        x = self.emb_dropout(x)

        x = self.input_layer(x)

        x = self.layer_norm(x)
        # SignJoey provides a mask of shape (B, 1, M), but BERT expects (B, M).
        mask = mask.squeeze(dim=1)
        # Because we cut off the head of BERT, we need to further process the mask.
        mask = self.bert_model.get_extended_attention_mask(mask, mask.shape, mask.device)
        x = self.encoder(x, attention_mask=mask)

        return x.last_hidden_state, None

    def __repr__(self):
        return "%s(num_layers=%r, num_heads=%r)" % (
            self.__class__.__name__,
            self.num_layers,
            self.encoder.config.num_attention_heads,
        )


class mBARTEncoder(Encoder):
    """
    Transformer Encoder
    """

    # pylint: disable=unused-argument
    def __init__(
            self,
            hidden_size: int = 768,
            num_layers: int = 3,
            emb_dropout: float = 0.1,
            freeze: bool = False,
            freeze_pt: str = None,
            pretrained_name: str = 'facebook/mbart-large-cc25',
            input_layer_init: str = None,
            **kwargs
    ):
        """
        Initializes the Transformer.
        :param hidden_size: hidden size and size of embeddings
        :param num_layers: number of layers to keep from BERT
        :param dropout: dropout probability for Transformer layers
        :param emb_dropout: Is applied to the input (word embeddings).
        :param freeze: freeze the parameters of the encoder during training
        :param pretrained_name: the name to pass to the Huggingface hub for the BERT model weights
        :param kwargs:
        """
        super(mBARTEncoder, self).__init__()

        self.config = MBartConfig.from_pretrained(pretrained_name)
        self.mbart_model = MBartModel.from_pretrained(pretrained_name)
        self.encoder = self.mbart_model.encoder
        # Make sure our configuration file is compatible with the pretrained model
        assert self.encoder.config.hidden_size == hidden_size

        # Only keep given number of layers
        orig_nr_layers = len(self.encoder.layers)
        for i in range(orig_nr_layers - 1, num_layers - 1, -1):
            self.encoder.layers[i] = mBARTIdentity()

        # Add a linear transformation to our embeddings before passing them to mBART.
        self.input_layer = nn.Linear(hidden_size, hidden_size, bias=False)
        if input_layer_init == 'orthogonal':
            torch.nn.init.orthogonal_(self.input_layer.weight)
        elif input_layer_init == 'normal':
            torch.nn.init.normal_(self.input_layer.weight)

        # Freeze if needed. We only freeze the attention and intermediate layers, but keep the
        #  linear transformations in the "BertOutput" layer.
        print(f'Freezing pre-trained transformer: {freeze_pt}')
        print(f'Freezing entire encoder: {freeze}')
        # Default behavior, also for freeze_pt == 'layer_norm_only', is to freeze everything except layer norm
        for name, p in self.encoder.named_parameters():
            name = name.lower()
            if 'layer_norm' not in name and 'layernorm' not in name and 'embed_positions' not in name:
                p.requires_grad = False
        if freeze_pt == 'finetune_ff':  # We already froze everything, now unfreeze ff
            for name, p in self.encoder.named_parameters():
                name = name.lower()
                if 'fc' in name:
                    p.requires_grad = True

        self.layer_norm = nn.LayerNorm(hidden_size, eps=1e-6)

        self._output_size = hidden_size
        self.num_layers = num_layers

    # pylint: disable=arguments-differ
    def forward(
            self, embed_src: Tensor, src_length: Tensor, mask: Tensor
    ) -> (Tensor, Tensor):
        """
        Pass the input (and mask) through each layer in turn.
        Applies a Transformer encoder to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param mask: indicates padding areas (zeros where padding), shape
            (batch_size, src_len, embed_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        x = self.input_layer(embed_src)
        # mBART includes positional encoding, so we don't compute them here.

        x = self.layer_norm(x)
        # Convert mask to format expected by mBART.
        mask = mask.squeeze(dim=1).type(torch.float32)
        # Use inputs_embeds because we already have embeddings.
        x = self.encoder(inputs_embeds=x, attention_mask=mask)

        return x.last_hidden_state, None

    def __repr__(self):
        return "%s(num_layers=%r, num_heads=%r)" % (
            self.__class__.__name__,
            self.num_layers,
            self.encoder.config.num_attention_heads,
        )
