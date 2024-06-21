import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import logging
from typing import Union


from tqdm import tqdm
from transformers.cache_utils import Cache

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils.parametrize as P
from torch.nn.utils.parametrizations import weight_norm
from transformers import LlamaModel, LlamaConfig

from ..utils.io import del_all


class LlamaMLP(nn.Module):
    def __init__(self, hidden_size, intermediate_size):
        super().__init__()
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
        self.act_fn = F.silu

    def forward(self, x):
        down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
        return down_proj
    
    
class GPT_warpper(nn.Module):
    def __init__(
        self, 
        gpt_config, 
        num_audio_tokens,
        num_text_tokens,
        num_vq=4,
        device="cpu",
    ):
        super().__init__()

        self.logger = logging.getLogger(__name__)
        self.device = device
        self.device_gpt = device if "mps" not in str(device) else "cpu"
        self.num_vq = num_vq

        self.gpt = self.build_model(gpt_config, self.device_gpt)      
        self.model_dim = self.gpt.config.hidden_size 
        self.emb_code = nn.ModuleList(
            [nn.Embedding(
                num_audio_tokens, self.model_dim, device=self.device_gpt,
            ) for _ in range(num_vq)],
        )
        self.emb_text = nn.Embedding(num_text_tokens, self.model_dim, device=self.device_gpt)

        self.head_text = weight_norm(
            nn.Linear(
                self.model_dim, num_text_tokens, bias=False, device=device,
            ),
            name='weight',
        )
        self.head_code = nn.ModuleList(
            [weight_norm(
                nn.Linear(
                    self.model_dim, num_audio_tokens, bias=False, device=device,
                ),
                name='weight',
            ) for _ in range(self.num_vq)],
        )

    def build_model(self, config, device):
        
        configuration = LlamaConfig(**config)
        model = LlamaModel(configuration)
        del model.embed_tokens
        
        return model.to(device)

    def get_emb(self, input_ids, text_mask):

        emb_text = self.emb_text(input_ids[text_mask][:, 0].to(self.device_gpt))

        text_mask_inv = ~text_mask
        masked_input_ids = input_ids[text_mask_inv].to(self.device_gpt)
        del text_mask_inv

        emb_code = [self.emb_code[i](masked_input_ids[:, i]) for i in range(self.num_vq)]
        emb_code = torch.stack(emb_code, 2).sum(2)

        emb = torch.zeros((input_ids.shape[:-1])+(emb_text.shape[-1],), device=emb_text.device, dtype=emb_text.dtype)
        emb[text_mask] = emb_text
        emb[~text_mask] = emb_code.to(emb.dtype)

        del emb_text, emb_code

        return emb

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, inputs_embeds=None, cache_position=None, **kwargs
    ):
        # With static cache, the `past_key_values` is None
        # TODO joao: standardize interface for the different Cache classes and remove of this if
        has_static_cache = False
        if past_key_values is None:
            past_key_values = getattr(self.gpt.layers[0].self_attn, "past_key_value", None)
            has_static_cache = past_key_values is not None

        past_length = 0
        if past_key_values is not None:
            if isinstance(past_key_values, Cache):
                past_length = cache_position[0] if cache_position is not None else past_key_values.get_seq_length()
                max_cache_length = (
                    torch.tensor(past_key_values.get_max_length(), device=input_ids.device)
                    if past_key_values.get_max_length() is not None
                    else None
                )
                cache_length = past_length if max_cache_length is None else torch.min(max_cache_length, past_length)
            # TODO joao: remove this `else` after `generate` prioritizes `Cache` objects
            else:
                cache_length = past_length = past_key_values[0][0].shape[2]
                max_cache_length = None

            # Keep only the unprocessed tokens:
            # 1 - If the length of the attention_mask exceeds the length of input_ids, then we are in a setting where
            # some of the inputs are exclusively passed as part of the cache (e.g. when passing input_embeds as
            # input)
            if attention_mask is not None and attention_mask.shape[1] > input_ids.shape[1]:
                input_ids = input_ids[:, -(attention_mask.shape[1] - past_length) :]
            # 2 - If the past_length is smaller than input_ids', then input_ids holds all input tokens. We can discard
            # input_ids based on the past_length.
            elif past_length < input_ids.shape[1]:
                input_ids = input_ids[:, past_length:]
            # 3 - Otherwise (past_length >= input_ids.shape[1]), let's assume input_ids only has unprocessed tokens.

            # If we are about to go beyond the maximum cache length, we need to crop the input attention mask.
            if (
                max_cache_length is not None
                and attention_mask is not None
                and cache_length + input_ids.shape[1] > max_cache_length
            ):
                attention_mask = attention_mask[:, -max_cache_length:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -input_ids.shape[1] :]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            # The `contiguous()` here is necessary to have a static stride during decoding. torchdynamo otherwise
            # recompiles graphs as the stride of the inputs is a guard. Ref: https://github.com/huggingface/transformers/pull/29114
            # TODO: use `next_tokens` directly instead.
            model_inputs = {"input_ids": input_ids.contiguous()}

        input_length = position_ids.shape[-1] if position_ids is not None else input_ids.shape[-1]
        if cache_position is None:
            cache_position = torch.arange(past_length, past_length + input_length, device=input_ids.device)
        else:
            cache_position = cache_position[-input_length:]

        if has_static_cache:
            past_key_values = None

        model_inputs.update(
            {
                "position_ids": position_ids,
                "cache_position": cache_position,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
            }
        )
        return model_inputs
    
    def generate(
        self, 
        emb, 
        inputs_ids, 
        temperature, 
        eos_token: Union[int, torch.Tensor], 
        attention_mask = None,
        max_new_token = 2048, 
        min_new_token = 0,
        LogitsWarpers = [],
        LogitsProcessors = [],
        infer_text=False,
        return_attn=False,
        return_hidden=False,
        stream=False,
    ):
        
        with torch.no_grad():   
        
            attentions = []
            hiddens = []
            
            start_idx, end_idx = inputs_ids.shape[1], torch.zeros(inputs_ids.shape[0], device=inputs_ids.device, dtype=torch.long)
            finish = torch.zeros(inputs_ids.shape[0], device=inputs_ids.device).bool()
            
            temperature = temperature.unsqueeze_(0).expand(inputs_ids.shape[0], -1).contiguous().view(-1, 1)
            # temperature = rearrange(temperature, "b n -> (b n) 1")

            attention_mask_cache = torch.ones((inputs_ids.shape[0], inputs_ids.shape[1]+max_new_token,), dtype=torch.bool, device=inputs_ids.device)
            if attention_mask is not None:
                attention_mask_cache[:, :attention_mask.shape[1]] = attention_mask

            with tqdm(total=max_new_token) as pbar:

                past_key_values = None

                for i in range(max_new_token):
                    model_input = self.prepare_inputs_for_generation(
                        inputs_ids, 
                        past_key_values, 
                        attention_mask_cache[:, :inputs_ids.shape[1]],
                        use_cache=True,
                    )

                    if i == 0:
                        model_input['inputs_embeds'] = emb
                    else:
                        inputs_ids_emb = model_input['input_ids'].to(self.device_gpt)
                        if infer_text:
                            model_input['inputs_embeds'] = self.emb_text(inputs_ids_emb[:,:,0])
                        else:
                            code_emb = [self.emb_code[i](inputs_ids_emb[:,:,i]) for i in range(self.num_vq)]
                            model_input['inputs_embeds'] = torch.stack(code_emb, 3).sum(3)
                        del inputs_ids_emb, model_input['input_ids']

                    outputs = self.gpt.forward(
                        attention_mask=model_input["attention_mask"].to(self.device_gpt),
                        position_ids=model_input["position_ids"].to(self.device_gpt),
                        past_key_values=model_input["past_key_values"],
                        inputs_embeds=model_input['inputs_embeds'].to(self.device_gpt),
                        use_cache=model_input['use_cache'],
                        output_attentions=return_attn,
                        cache_position=model_input['cache_position'].to(self.device_gpt),
                    )
                    del_all(model_input)
                    attentions.append(outputs.attentions)
                    hidden_states = outputs[0].to(self.device) # 🐻
                    past_key_values = outputs.past_key_values
                    del outputs
                    if return_hidden:
                        hiddens.append(hidden_states[:, -1])

                    with P.cached():
                        if infer_text:
                            logits = self.head_text(hidden_states)
                        else:
                            logits = torch.stack([self.head_code[i](hidden_states) for i in range(self.num_vq)], 3)

                    logits = logits[:, -1].float()

                    if not infer_text:
                        # logits = rearrange(logits, "b c n -> (b n) c")
                        logits = logits.permute(0, 2, 1)
                        logits = logits.reshape(-1, logits.size(2))
                        # logits_token = rearrange(inputs_ids[:, start_idx:], "b c n -> (b n) c")
                        inputs_ids_sliced = inputs_ids[:, start_idx:].permute(0, 2, 1)
                        logits_token = inputs_ids_sliced.reshape(
                            inputs_ids_sliced.size(0)*inputs_ids_sliced.size(1), -1,
                        )
                    else:
                        logits_token = inputs_ids[:, start_idx:, 0]

                    logits = logits / temperature

                    for logitsProcessors in LogitsProcessors:
                        logits = logitsProcessors(logits_token, logits)

                    for logitsWarpers in LogitsWarpers:
                        logits = logitsWarpers(logits_token, logits)

                    del logits_token

                    if i < min_new_token:
                        logits[:, eos_token] = -torch.inf

                    scores = F.softmax(logits, dim=-1)

                    del logits

                    idx_next = torch.multinomial(scores, num_samples=1).to(finish.device)

                    if not infer_text:
                        # idx_next = rearrange(idx_next, "(b n) 1 -> b n", n=self.num_vq)
                        idx_next = idx_next.view(-1, self.num_vq)
                        finish_or = (idx_next == eos_token).any(1)
                        finish |= finish_or
                        del finish_or
                        inputs_ids = torch.cat([inputs_ids, idx_next.unsqueeze(1)], 1)
                    else:
                        finish_or = (idx_next == eos_token).any(1)
                        finish |= finish_or
                        del finish_or
                        inputs_ids = torch.cat([inputs_ids, idx_next.unsqueeze(-1).expand(-1, -1, self.num_vq)], 1)

                    del idx_next

                    end_idx += (~finish).int().to(end_idx.device)
                    if stream:
                        if end_idx % 24 and not finish.all():
                            continue
                        y_inputs_ids = [inputs_ids[idx, start_idx: start_idx+i] for idx, i in enumerate(end_idx.int())]
                        y_inputs_ids = [i[:, 0] for i in y_inputs_ids] if infer_text else y_inputs_ids
                        y_hiddens = [[]]
                        if return_hidden:
                            y_hiddens = torch.stack(hiddens, 1)
                            y_hiddens = [y_hiddens[idx, :i] for idx, i in enumerate(end_idx.int())]
                        yield {
                            'ids': y_inputs_ids, 
                            'attentions': attentions,
                            'hiddens':y_hiddens,
                        }

                    if finish.all():
                        pbar.update(max_new_token-i-1)
                        break
                    pbar.update(1)

            inputs_ids = [inputs_ids[idx, start_idx: start_idx+i] for idx, i in enumerate(end_idx.int())]
            inputs_ids = [i[:, 0] for i in inputs_ids] if infer_text else inputs_ids
            
            if return_hidden:
                hiddens = torch.stack(hiddens, 1)
                hiddens = [hiddens[idx, :i] for idx, i in enumerate(end_idx.int())]
                    
            if not finish.all():
                self.logger.warn(f'Incomplete result. hit max_new_token: {max_new_token}')

            del finish

            yield {
                'ids': inputs_ids, 
                'attentions': attentions,
                'hiddens':hiddens,
            }
