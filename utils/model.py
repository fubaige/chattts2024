
import torch
from einops import rearrange

import ChatTTS
import ChatTTS.model.dvae


def encode(
    chat: ChatTTS.Chat,
    audio_mel_specs: torch.Tensor,  # (batch_size, audio_len*2, 100)
) -> tuple[torch.Tensor, torch.Tensor]:
    dvae: ChatTTS.model.dvae.DVAE = chat.pretrain_models['dvae']
    vq: ChatTTS.model.dvae.GFSQ = dvae.vq_layer

    batch_size = audio_mel_specs.shape[0]
    audio_len = audio_mel_specs.shape[1] // 2

    latents = torch.zeros(  # TODO: placeholder
        (batch_size, audio_len, 1024),
        dtype=audio_mel_specs.dtype,
        device=audio_mel_specs.device,
    )   # (batch_size, audio_len, audio_dim)

    # feat shape (batch_size, audio_len, audio_dim)
    # ind shape (2, batch_size, audio_len, 2)
    feat, ind = vq.quantizer(latents)
    audio_quantized_latents = feat   # (batch_size, audio_len, audio_dim)
    audio_input_ids = rearrange(   # (batch_size, audio_len, num_vq)
        ind, "g b t r ->b t (g r)",
    )
    return audio_quantized_latents, audio_input_ids
