"""Minimal SpEx+ inference model.

The architecture is derived from ClearerVoice-Studio's audio-only target
speaker extraction recipe at commit 6b3774dc79c46ae8bed2a4fa5f706f0ac8c75c61.
The original implementation is Copyright 2020 Meng Ge and MIT licensed.
Only the inference path required by this App is retained here.
"""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as functional


class ChannelWiseLayerNorm(nn.LayerNorm):
    def forward(self, value: torch.Tensor) -> torch.Tensor:
        if value.dim() != 3:
            raise RuntimeError("ChannelWiseLayerNorm expects a three-dimensional tensor")
        return super().forward(value.transpose(1, 2)).transpose(1, 2)


class GlobalChannelLayerNorm(nn.Module):
    def __init__(self, channels: int, epsilon: float = 1e-5) -> None:
        super().__init__()
        self.epsilon = epsilon
        self.beta = nn.Parameter(torch.zeros(channels, 1))
        self.gamma = nn.Parameter(torch.ones(channels, 1))

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        mean = torch.mean(value, (1, 2), keepdim=True)
        variance = torch.mean((value - mean) ** 2, (1, 2), keepdim=True)
        return self.gamma * (value - mean) / torch.sqrt(variance + self.epsilon) + self.beta


def _normalization(kind: str, channels: int) -> nn.Module:
    if kind == "cLN":
        return ChannelWiseLayerNorm(channels, elementwise_affine=True)
    if kind == "gLN":
        return GlobalChannelLayerNorm(channels)
    if kind == "BN":
        return nn.BatchNorm1d(channels)
    raise ValueError(f"Unsupported SpEx+ normalization: {kind}")


class Conv1D(nn.Conv1d):
    def forward(self, value: torch.Tensor) -> torch.Tensor:
        if value.dim() not in (2, 3):
            raise RuntimeError("Conv1D expects a two- or three-dimensional tensor")
        return super().forward(value if value.dim() == 3 else value.unsqueeze(1))


class ConvTranspose1D(nn.ConvTranspose1d):
    def forward(self, value: torch.Tensor, original_length: int) -> torch.Tensor:
        if value.dim() not in (2, 3):
            raise RuntimeError("ConvTranspose1D expects a two- or three-dimensional tensor")
        decoded = super().forward(value if value.dim() == 3 else value.unsqueeze(1))
        difference = original_length - decoded.size(-1)
        if difference >= 0:
            return functional.pad(decoded, (0, difference))
        return decoded[..., :original_length]


class ResidualBlock(nn.Module):
    def __init__(self, input_channels: int, output_channels: int) -> None:
        super().__init__()
        self.conv1 = nn.Conv1d(input_channels, output_channels, 1, bias=False)
        self.conv2 = nn.Conv1d(output_channels, output_channels, 1, bias=False)
        self.norm1 = nn.BatchNorm1d(output_channels)
        self.norm2 = nn.BatchNorm1d(output_channels)
        self.activation1 = nn.PReLU()
        self.activation2 = nn.PReLU()
        self.pool = nn.MaxPool1d(3)
        self.downsample = (
            nn.Conv1d(input_channels, output_channels, 1, bias=False)
            if input_channels != output_channels
            else None
        )

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        residual = value if self.downsample is None else self.downsample(value)
        value = self.activation1(self.norm1(self.conv1(value)))
        value = self.norm2(self.conv2(value))
        return self.pool(self.activation2(value + residual))


class TemporalBlock(nn.Module):
    def __init__(
        self,
        input_channels: int = 256,
        convolution_channels: int = 512,
        kernel_size: int = 3,
        dilation: int = 1,
        normalization: str = "gLN",
        causal: bool = False,
    ) -> None:
        super().__init__()
        self.project = Conv1D(input_channels, convolution_channels, 1)
        self.activation1 = nn.PReLU()
        self.norm1 = _normalization(normalization, convolution_channels)
        self.padding = dilation * (kernel_size - 1) if causal else dilation * (kernel_size - 1) // 2
        self.depthwise = nn.Conv1d(
            convolution_channels,
            convolution_channels,
            kernel_size,
            groups=convolution_channels,
            padding=self.padding,
            dilation=dilation,
        )
        self.activation2 = nn.PReLU()
        self.norm2 = _normalization(normalization, convolution_channels)
        self.output = nn.Conv1d(convolution_channels, input_channels, 1)
        self.causal = causal

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        residual = value
        value = self.norm1(self.activation1(self.project(value)))
        value = self.depthwise(value)
        if self.causal and self.padding:
            value = value[..., :-self.padding]
        value = self.output(self.norm2(self.activation2(value)))
        return residual + value


class ConditionedTemporalBlock(TemporalBlock):
    def __init__(self, *args, speaker_channels: int = 256, **kwargs) -> None:
        input_channels = int(kwargs.pop("input_channels", 256))
        super().__init__(*args, input_channels=input_channels + speaker_channels, **kwargs)
        self.output = nn.Conv1d(
            int(kwargs.get("convolution_channels", 512)),
            input_channels,
            1,
        )
        self.input_channels = input_channels

    def forward(self, value: torch.Tensor, speaker: torch.Tensor) -> torch.Tensor:
        residual = value
        repeated = speaker.unsqueeze(-1).expand(-1, -1, value.shape[-1])
        conditioned = torch.cat((value, repeated), dim=1)
        conditioned = self.norm1(self.activation1(self.project(conditioned)))
        conditioned = self.depthwise(conditioned)
        if self.causal and self.padding:
            conditioned = conditioned[..., :-self.padding]
        conditioned = self.output(self.norm2(self.activation2(conditioned)))
        return residual + conditioned


class SpExPlus(nn.Module):
    """Audio-only, non-causal SpEx+ model used by the official checkpoint."""

    def __init__(self) -> None:
        super().__init__()
        length = 20
        channels = 256
        bottleneck = 256
        hidden = 512
        blocks = 8
        normalization = "gLN"

        self.short_length = length
        self.middle_length = 80
        self.long_length = 160
        stride = length // 2
        self.encoder_1d_short = Conv1D(1, channels, length, stride=stride)
        self.encoder_1d_middle = Conv1D(1, channels, self.middle_length, stride=stride)
        self.encoder_1d_long = Conv1D(1, channels, self.long_length, stride=stride)
        self.ln = ChannelWiseLayerNorm(3 * channels)
        self.proj = Conv1D(3 * channels, bottleneck, 1)

        conditioned = {
            "input_channels": bottleneck,
            "speaker_channels": 256,
            "convolution_channels": hidden,
            "kernel_size": 3,
            "normalization": normalization,
            "causal": False,
            "dilation": 1,
        }
        plain = {
            "input_channels": bottleneck,
            "convolution_channels": hidden,
            "kernel_size": 3,
            "normalization": normalization,
            "causal": False,
        }
        for repeat in range(1, 5):
            setattr(self, f"conv_block_{repeat}", ConditionedTemporalBlock(**conditioned))
            setattr(
                self,
                f"conv_block_{repeat}_other",
                nn.Sequential(
                    *[TemporalBlock(**plain, dilation=2**index) for index in range(1, blocks)]
                ),
            )

        self.mask1 = Conv1D(bottleneck, channels, 1)
        self.mask2 = Conv1D(bottleneck, channels, 1)
        self.mask3 = Conv1D(bottleneck, channels, 1)
        self.decoder_1d_1 = ConvTranspose1D(channels, 1, length, stride=stride, bias=True)
        self.decoder_1d_2 = ConvTranspose1D(
            channels, 1, self.middle_length, stride=stride, bias=True
        )
        self.decoder_1d_3 = ConvTranspose1D(
            channels, 1, self.long_length, stride=stride, bias=True
        )
        self.aux_enc3 = nn.Sequential(
            ChannelWiseLayerNorm(3 * channels),
            Conv1D(3 * channels, 256, 1),
            ResidualBlock(256, 256),
            ResidualBlock(256, 512),
            ResidualBlock(512, 512),
            Conv1D(512, 256, 1),
        )
        self.pred_linear = nn.Linear(256, 101)

    def _encode_scales(self, value: torch.Tensor) -> tuple[torch.Tensor, ...]:
        short = functional.relu(self.encoder_1d_short(value))
        frames = short.shape[-1]
        original = value.shape[-1]
        middle_length = (frames - 1) * (self.short_length // 2) + self.middle_length
        long_length = (frames - 1) * (self.short_length // 2) + self.long_length
        middle = functional.relu(
            self.encoder_1d_middle(functional.pad(value, (0, middle_length - original)))
        )
        long = functional.relu(
            self.encoder_1d_long(functional.pad(value, (0, long_length - original)))
        )
        return short, middle, long

    def forward(
        self, mixture: torch.Tensor, reference: torch.Tensor, reference_length: torch.Tensor
    ) -> torch.Tensor:
        if mixture.dim() == 1:
            mixture = mixture.unsqueeze(0)
        if reference.dim() == 1:
            reference = reference.unsqueeze(0)
        original_length = mixture.shape[-1]
        short, middle, long = self._encode_scales(mixture)
        features = self.proj(self.ln(torch.cat((short, middle, long), dim=1)))

        ref_short, ref_middle, ref_long = self._encode_scales(reference)
        speaker = self.aux_enc3(torch.cat((ref_short, ref_middle, ref_long), dim=1))
        speaker_frames = (reference_length - self.short_length) // (self.short_length // 2) + 1
        speaker_frames = torch.clamp(((speaker_frames // 3) // 3) // 3, min=1)
        speaker = torch.sum(speaker, dim=-1) / speaker_frames.view(-1, 1).float()

        for repeat in range(1, 5):
            features = getattr(self, f"conv_block_{repeat}")(features, speaker)
            features = getattr(self, f"conv_block_{repeat}_other")(features)

        masks = (
            functional.relu(self.mask1(features)),
            functional.relu(self.mask2(features)),
            functional.relu(self.mask3(features)),
        )
        decoded = self.decoder_1d_1(short * masks[0], original_length).squeeze(1)
        return decoded[..., :original_length]


def load_spex_plus(checkpoint_path: str) -> SpExPlus:
    """Load and validate the pinned ClearerVoice-Studio checkpoint."""
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    raw_state = checkpoint.get("model", checkpoint)
    prefix = "module.sep_network."
    replacements = (
        (".conv1x1.", ".project."),
        (".prelu1.", ".activation1."),
        (".lnorm1.", ".norm1."),
        (".dconv.", ".depthwise."),
        (".prelu2.", ".activation2."),
        (".lnorm2.", ".norm2."),
        (".sconv.", ".output."),
        (".batch_norm1.", ".norm1."),
        (".batch_norm2.", ".norm2."),
        (".conv_downsample.", ".downsample."),
    )
    state = {}
    for raw_key, value in raw_state.items():
        if not raw_key.startswith(prefix):
            continue
        key = raw_key[len(prefix) :]
        for old, new in replacements:
            key = key.replace(old, new)
        state[key] = value
    model = SpExPlus()
    model.load_state_dict(state, strict=True)
    model.eval()
    return model
