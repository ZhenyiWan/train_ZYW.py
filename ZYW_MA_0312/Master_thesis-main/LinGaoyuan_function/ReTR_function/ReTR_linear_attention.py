"""
Linear Transformer proposed in "Transformers are RNNs: Fast Autoregressive Transformers with Linear Attention"
Modified from: https://github.com/idiap/fast-transformers/blob/master/fast_transformers/attention/linear_attention.py
"""

import torch
from torch.nn import Module, Dropout


def elu_feature_map(x):
    #ELU（指数线性单元）激活函数，并在结果上加 1
    return torch.nn.functional.elu(x) + 1


class LinearAttention(Module):
    def __init__(self, eps=1e-6):
        super().__init__()
        self.feature_map = elu_feature_map
        self.eps = eps#小常数，防止除0错误

    def forward(self, queries, keys, values, q_mask=None, kv_mask=None):
        """ Multi-Head linear attention proposed in "Transformers are RNNs"
        Args:
            queries: [N, L, H, D]
            keys: [N, S, H, D]
            values: [N, S, H, D]
            q_mask: [N, L]
            kv_mask: [N, S]
        Returns:
            queried_values: (N, L, H, D)
        """
        # Zhenyi Wan [2025/3/12] ELU activation, add additional 1 to the result
        Q = self.feature_map(queries)
        K = self.feature_map(keys)

        # set padded position to zero
        if q_mask is not None:
            Q = Q * q_mask[:, :, None, None]
        if kv_mask is not None:
            K = K * kv_mask[:, :, None, None]
            values = values * kv_mask[:, :, None, None]
        v_length = values.size(1)
        # Zhenyi Wan [2025/3/12] To prevent values from overflowing (especially at FP16 precision),
        # values are scaled according to its length. By dividing by v_length, the value is kept stable.
        values = values / v_length  # prevent fp16 overflow
        KV = torch.einsum("nshd,nshv->nhdv", K, values)  # (S,D)' @ S,V #NHDV
        Z = 1 / (torch.einsum("nlhd,nhd->nlh", Q, K.sum(dim=1)) + self.eps)
        queried_values = torch.einsum("nlhd,nhdv,nlh->nlhv", Q, KV, Z) * v_length

        return queried_values.contiguous()


class FullAttention(Module):
    def __init__(self, use_dropout=False, attention_dropout=0.1):
        super().__init__()
        self.use_dropout = use_dropout
        self.dropout = Dropout(attention_dropout)

    def forward(self, queries, keys, values, q_mask=None, kv_mask=None):
        """ Multi-head scaled dot-product attention, a.k.a full attention.
        Args:
            queries: [N, L, H, D]
            keys: [N, S, H, D]
            values: [N, S, H, D]
            q_mask: [N, L]
            kv_mask: [N, S]
        Returns:
            queried_values: (N, L, H, D)
        """

        # Compute the unnormalized attention and apply the masks
        QK = torch.einsum("nlhd,nshd->nlsh", queries, keys)# Zhenyi Wan [2025/3/17] (n_rand, n_samples+1, n_samples+1, nhead) decoder (N_rand, 1, N_samples, 1)
        # Zhenyi Wan [2025/3/17] mask is a under triangle matrix.
        if kv_mask is not None:
            # Zhenyi Wan [2025/3/17] the right upper triangle matrix of QK is filled with -inf.
            QK.masked_fill_(~(q_mask[:, :, :, None].bool()), float('-inf'))
#            QK.masked_fill_(~(q_mask[:, :, None, None] * kv_mask[:, None, :, None]), float('-inf'))

        # Compute the attention and the weighted averag/e
        softmax_temp = 1. / queries.size(3)**.5  # 1/sqrt(D)
        self.A = torch.softmax(softmax_temp * QK, dim=2)# Zhenyi Wan [2025/3/17] Attention Matrix (n_rand, n_samples+1, n_samples+1, nhead) decoder (N_rand, 1, N_samples, 1)
        if self.use_dropout:
            A = self.dropout(self.A)

        queried_values = torch.einsum("nlsh,nshd->nlhd", self.A, values)

        return queried_values.contiguous()



class CosineAttention(Module):
    def __init__(self, use_dropout=False, attention_dropout=0.1):
        super().__init__()
        self.use_dropout = use_dropout
        self.dropout = Dropout(attention_dropout)
        self.temp_scale = torch.nn.Parameter(torch.ones(1))

    def cosine_similarity(self,queries,keys):
        queries = queries/queries.norm(dim=-1,keepdim=True)#[N, L, H, D]
        keys = keys/keys.norm(dim=-1,keepdim=True)#[N, S, H, D]
        #lang_feats = lang_feats/lang_feats.norm(dim=2,keepdim=True)
        similarity = torch.einsum("nlhd,nshd->nlsh", queries, keys) #n,l,s,h
        return similarity

    def forward(self, queries, keys, values, q_mask=None, kv_mask=None):
        """ Multi-head scaled dot-product attention, a.k.a full attention.
        Args:
            queries: [N, L, H, D]
            keys: [N, S, H, D]
            values: [N, S, H, D]
            q_mask: [N, L]
            kv_mask: [N, S]
        Returns:
            queried_values: (N, L, H, D)
        """

        # Compute the unnormalized attention and apply the masks
        QK = self.cosine_similarity(queries, keys)
        if kv_mask is not None:
            QK.masked_fill_(~(q_mask[:, :, :, None].bool()), float('-inf'))

        # Compute the attention and the weighted average
        self.A = torch.softmax( QK /self.temp_scale, dim=2)
        if self.use_dropout:
            A = self.dropout(self.A)

        queried_values = torch.einsum("nlsh,nshd->nlhd", self.A, values)

        return queried_values.contiguous()

class LearnedAttention(Module):
    def __init__(self,dim, use_dropout=False, attention_dropout=0.1):
        super().__init__()
        self.use_dropout = use_dropout
        self.dropout = Dropout(attention_dropout)
        self.weighted_vector = torch.nn.Linear(2*dim,1)

    def forward(self, queries, keys, values, q_mask=None, kv_mask=None):
        """ Multi-head scaled dot-product attention, a.k.a full attention.
        Args:
            queries: [N, L, H, D]
            keys: [N, S, H, D]
            values: [N, S, H, D]
            q_mask: [N, L]
            kv_mask: [N, S]
        Returns:
            queried_values: (N, L, H, D)
        """
        # Compute the unnormalized attention and apply the masks
        QK = torch.cat((queries.repeat(1,keys.shape[1],1,1), keys),dim=-1).permute(0,2,1,3)
        self.A = torch.softmax(self.weighted_vector(QK), dim=2)
#        self.A = F.relu(self.weighted_vector(QK))
        if self.use_dropout:
            A = self.dropout(self.A)

        queried_values = torch.einsum("nlsh,nshd->nlhd", self.A, values)

        return queried_values.contiguous()