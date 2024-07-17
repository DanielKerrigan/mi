"""Sparse autoencoder model."""

from os import PathLike
from typing import Any, Callable, Literal, Union

import einops
import torch
import torch.nn as nn
from typing_extensions import Self

from sparse_autoencoder.config import Config


def LN(
    x: torch.Tensor, eps: float = 1e-5
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    mu = x.mean(dim=-1, keepdim=True)
    x = x - mu
    std = x.std(dim=-1, keepdim=True)
    x = x / (std + eps)
    return x, mu, std


class SAE(nn.Module):
    """Sparse autoencoder

    Implements:
        latents = relu(topk(encoder(x - b_dec) + b_enc))
        recons = decoder(latents) + b_dec
    """

    def __init__(self, cfg: Config) -> None:
        super().__init__()

        self.dtype = getattr(torch, cfg.dtype)
        self.device = torch.device(cfg.device)

        self.cfg = cfg

        self.b_dec = nn.Parameter(torch.zeros(cfg.d_in, device=self.device))
        self.b_enc = nn.Parameter(torch.zeros(cfg.d_sae, device=self.device))

        self.W_dec = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(
                    cfg.d_sae,
                    cfg.d_in,
                    dtype=self.dtype,
                    device=self.device,
                )
            )
        )
        self.set_decoder_norm_to_unit_norm()

        self.W_enc = nn.Parameter(self.W_dec.t().clone().contiguous())

        self.topk = TopK(cfg.k)
        self.aux_topk = TopK(cfg.aux_k)

        self.stats_last_nonzero: torch.Tensor
        self.register_buffer(
            "stats_last_nonzero",
            torch.zeros(cfg.d_sae, dtype=torch.long, device=self.device),
        )

    def auxk_masker(self, x: torch.Tensor) -> torch.Tensor:
        """mask dead neurons"""
        dead_mask = self.stats_last_nonzero > self.cfg.dead_steps_threshold
        x.data *= dead_mask
        return x

    def encode_pre_act(self, x: torch.Tensor) -> torch.Tensor:
        """
        :param x: input data (shape: [batch, n_inputs])
        :return: autoencoder latents before activation (shape: [batch, n_latents])
        """
        x = x - self.b_dec
        latents_pre_act = x @ self.W_enc + self.b_enc
        return latents_pre_act

    def preprocess(
        self, x: torch.Tensor
    ) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
        """Mean center, standard"""
        if not self.cfg.normalize:
            return x, dict()
        x, mu, std = LN(x)
        return x, dict(mu=mu, std=std)

    def encode(self, x: torch.Tensor) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
        """
        :param x: input data (shape: [batch, n_inputs])
        :return: autoencoder latents (shape: [batch, n_latents])
        """
        x, info = self.preprocess(x)
        return self.topk(self.encode_pre_act(x)), info

    def decode(
        self, latents: torch.Tensor, info: dict[str, Any] | None = None
    ) -> torch.Tensor:
        """
        :param latents: autoencoder latents (shape: [batch, n_latents])
        :return: reconstructed data (shape: [batch, n_inputs])
        """
        recontructed = latents @ self.W_dec + self.b_dec

        if self.cfg.normalize and info:
            recontructed = recontructed * info["std"] + info["mu"]

        return recontructed

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """
        :param x: input data (shape: [batch, n_inputs])
        :return:  autoencoder latents pre activation (shape: [batch, n_latents])
                  autoencoder latents (shape: [batch, n_latents])
                  reconstructed data (shape: [batch, n_inputs])
        """
        x, info = self.preprocess(x)
        latents_pre_act = self.encode_pre_act(x)
        latents = self.topk(latents_pre_act)
        recons = self.decode(latents, info)

        # set all indices of self.stats_last_nonzero where (latents != 0) to 0
        self.stats_last_nonzero *= (latents == 0).all(dim=0).long()
        self.stats_last_nonzero += 1

        aux_latents = self.aux_topk(self.auxk_masker(latents_pre_act))
        aux_recons = self.decode(aux_latents, info)

        return recons, aux_recons

    @torch.no_grad()
    def set_decoder_norm_to_unit_norm(self):
        """
        Set decoder weights to have unit norm.
        """
        self.W_dec.data /= self.W_dec.data.norm(dim=1, keepdim=True)

    @torch.no_grad()
    def remove_gradient_parallel_to_decoder_directions(self):
        """
        Update grads so that they remove the parallel component
            (d_sae, d_in) shape
        """
        assert self.W_dec.grad is not None  # keep pyright happy

        # The below code is equivalent to
        # parallel_component = (self.W_dec.grad * self.W_dec.data).sum(
        #     dim=1, keepdim=True
        # )
        # self.W_dec.grad -= parallel_component * self.W_dec.data

        parallel_component = einops.einsum(
            self.W_dec.grad,
            self.W_dec.data,
            "d_sae d_in, d_sae d_in -> d_sae",
        )

        self.W_dec.grad -= einops.einsum(
            parallel_component,
            self.W_dec.data,
            "d_sae, d_sae d_in -> d_sae d_in",
        )

    def save(self, path: Union[str, PathLike]):
        """Save model to path."""
        torch.save([self.cfg, self.state_dict()], path)

    @classmethod
    def load(
        cls,
        path: Union[str, PathLike],
        device: Literal["cpu", "cuda"],
    ) -> Self:
        """Load model from path."""
        config, state = torch.load(path, map_location=device)

        config.device = device

        model = cls(config)
        model.load_state_dict(state)

        return model


class TopK(nn.Module):
    """TopK activation."""

    def __init__(self, k: int, postact_fn: Callable = nn.ReLU()) -> None:
        super().__init__()
        self.k = k
        self.postact_fn = postact_fn

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass."""
        topk = torch.topk(x, k=self.k, dim=-1)
        values = self.postact_fn(topk.values)
        # make all other values 0
        result = torch.zeros_like(x)
        result.scatter_(-1, topk.indices, values)
        return result
