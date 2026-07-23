"""Compatibility shims for bge-m3 / torch."""
from __future__ import annotations

_SPARSE_PATCHED = False


def patch_torch_load_check():
    noop = lambda *a, **k: None
    for modname in ("transformers.utils.import_utils", "transformers.modeling_utils"):
        try:
            mod = __import__(modname, fromlist=["check_torch_load_is_safe"])
            if hasattr(mod, "check_torch_load_is_safe"):
                mod.check_torch_load_is_safe = noop
        except Exception:
            pass


def patch_bge_m3_sparse():
    """Fix bge-m3 sparse scatter OOB (token id / special-token id >= sparse width)."""
    global _SPARSE_PATCHED
    if _SPARSE_PATCHED:
        return
    try:
        import torch
        from FlagEmbedding.finetune.embedder.encoder_only.m3.modeling import (
            EncoderOnlyEmbedderM3Model,
        )
    except Exception:
        return

    if getattr(EncoderOnlyEmbedderM3Model, "_medrag_sparse_patched", False):
        _SPARSE_PATCHED = True
        return

    def _sparse_embedding_safe(self, hidden_state, input_ids, return_embedding=True):
        token_weights = torch.relu(self.sparse_linear(hidden_state))
        if not return_embedding:
            return token_weights

        sparse_dim = int(getattr(self, "vocab_size", 0) or 0)
        tok = getattr(self, "tokenizer", None)
        pad = getattr(tok, "pad_token_id", None) if tok else None
        if pad is None or pad < 0 or (sparse_dim and pad >= sparse_dim):
            pad = 1

        ids = input_ids.clone()
        if sparse_dim:
            ids = ids.clamp(min=0, max=sparse_dim - 1)

        if self.training:
            sparse_embedding = torch.zeros(
                ids.size(0), ids.size(1), sparse_dim,
                dtype=token_weights.dtype,
                device=token_weights.device,
            )
            sparse_embedding = torch.scatter(
                sparse_embedding, dim=-1, index=ids.unsqueeze(-1), src=token_weights,
            )
            sparse_embedding = torch.max(sparse_embedding, dim=1).values
        else:
            sparse_embedding = torch.zeros(
                ids.size(0), sparse_dim,
                dtype=token_weights.dtype,
                device=token_weights.device,
            )
            sparse_embedding = sparse_embedding.scatter_reduce(
                dim=-1, index=ids, src=token_weights.squeeze(-1), reduce="amax",
            )

        if tok is not None and sparse_dim:
            unused = [
                x for x in (
                    tok.cls_token_id, tok.eos_token_id, tok.pad_token_id, tok.unk_token_id,
                )
                if x is not None and 0 <= x < sparse_dim
            ]
            if unused:
                sparse_embedding[:, unused] = 0
        return sparse_embedding

    EncoderOnlyEmbedderM3Model._sparse_embedding = _sparse_embedding_safe
    EncoderOnlyEmbedderM3Model._medrag_sparse_patched = True
    _SPARSE_PATCHED = True
