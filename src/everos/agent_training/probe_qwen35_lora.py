"""Small, offline, synthetic Qwen3.5 4-bit LoRA training and reload probe."""
from __future__ import annotations
import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--max-length', type=int, default=128)
    parser.add_argument('--steps', type=int, default=5)
    parser.add_argument('--context-repeats', type=int, default=0)
    parser.add_argument('--dynamic-padding', action='store_true', help='Batch size one: do not pad to max length')
    parser.add_argument('--low-memory-setup', action='store_true', help='Freeze base without PEFT fp32 upcast; experimental')
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit('Output directory exists; choose a new path.')
    if args.max_length < 32 or args.steps < 1:
        raise SystemExit('max-length must be >=32 and steps >=1')
    args.output_dir.mkdir(parents=True)
    result = {'success':False, 'kind':'synthetic 4-bit QLoRA smoke test', 'max_length':args.max_length,'steps_requested':args.steps}
    try:
        import torch
        import transformers
        import peft
        import bitsandbytes
        import accelerate
        from transformers import AutoTokenizer, AutoModelForImageTextToText, BitsAndBytesConfig
        from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
        torch.manual_seed(42)
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable')
        result.update(torch=torch.__version__, cuda=torch.version.cuda, transformers=transformers.__version__, peft=peft.__version__, bitsandbytes=bitsandbytes.__version__, accelerate=accelerate.__version__, gpu=torch.cuda.get_device_name(0))
        torch.cuda.reset_peak_memory_stats()
        free, total = torch.cuda.mem_get_info()
        result.update(gpu_free_before_mib=round(free / 2**20),gpu_total_mib=round(total / 2**20))
        model_path = '/root/.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B/snapshots/2fc06364715b967f1860aea9cf38778875588b17'
        result['model_path']=model_path
        tokenizer=AutoTokenizer.from_pretrained(model_path,local_files_only=True)
        quant=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_quant_type='nf4',bnb_4bit_compute_dtype=torch.float16,bnb_4bit_use_double_quant=True)
        def load_base():
            return AutoModelForImageTextToText.from_pretrained(model_path,local_files_only=True,dtype=torch.float16,quantization_config=quant,device_map={'':0},attn_implementation='eager')
        start=time.monotonic()
        model=load_base()
        model.config.use_cache=False
        if args.low_memory_setup:
            for parameter in model.parameters():
                parameter.requires_grad_(False)
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
            result['base_setup'] = 'manual freeze, fp16 nonquantized weights, non-reentrant checkpointing'
        else:
            model=prepare_model_for_kbit_training(model,use_gradient_checkpointing=True,gradient_checkpointing_kwargs={'use_reentrant':False})
            result['base_setup'] = 'PEFT prepare_model_for_kbit_training'
        cfg=LoraConfig(r=8,lora_alpha=16,lora_dropout=0.0,bias='none',task_type='CAUSAL_LM',target_modules=r'.*language_model\.layers\.\d+\.self_attn\.(q_proj|v_proj)')
        model=get_peft_model(model,cfg)
        named=[(n,p) for n,p in model.named_parameters() if p.requires_grad]
        if not named or any('lora_' not in n for n,p in named):
            raise RuntimeError('unexpected trainable parameters')
        result['trainable_parameter_count']=sum(p.numel() for _,p in named)
        result['trainable_parameter_names']=[n for n,_ in named]
        result['parameter_devices']=sorted({str(p.device) for p in model.parameters()})
        examples=[('请只用一句话说出 2 加 3 的结果。','2 加 3 等于 5。'),('用户问北京天气，输出 weather 工具及 city 参数。','{"name":"weather","arguments":{"city":"北京"}}')]
        prepared=[]
        result['context_repeats']=args.context_repeats
        for question,answer in examples:
            question = ('历史工具反馈：此前操作已完成，可以继续下一步。\n' * args.context_repeats) + question
            prefix=tokenizer.apply_chat_template([{'role':'user','content':question}],tokenize=True,add_generation_prompt=True,enable_thinking=False)['input_ids']
            full=tokenizer.apply_chat_template([{'role':'user','content':question},{'role':'assistant','content':answer}],tokenize=True,add_generation_prompt=False,enable_thinking=False)['input_ids']
            if full[:len(prefix)] != prefix or len(full)>args.max_length:
                raise RuntimeError('template prefix mismatch or sample exceeds length')
            n=len(full); pad=0 if args.dynamic_padding else args.max_length-n
            prepared.append({'input_ids':torch.tensor([full+[tokenizer.pad_token_id]*pad],device='cuda'), 'attention_mask':torch.tensor([[1]*n+[0]*pad],device='cuda'), 'labels':torch.tensor([[-100]*len(prefix)+full[len(prefix):]+[-100]*pad],device='cuda')})
        result['dynamic_padding']=args.dynamic_padding
        result['sample_tensor_lengths']=[int(e['input_ids'].shape[-1]) for e in prepared]
        result['sample_actual_tokens']=[int(e['attention_mask'].sum().item()) for e in prepared]
        result['load_and_setup_seconds']=round(time.monotonic()-start,3)
        optimizer=torch.optim.AdamW((p for _,p in named),lr=2e-4)
        initial={n:p.detach().cpu().clone() for n,p in named}
        losses=[]; gradients=[];model.train();start=time.monotonic()
        for i in range(args.steps):
            optimizer.zero_grad(set_to_none=True)
            output=model(**prepared[i%len(prepared)],use_cache=False)
            loss=output.loss
            if not torch.isfinite(loss):raise RuntimeError('nonfinite loss')
            loss.backward()
            grad_norm=sum(float(p.grad.detach().float().norm().item()) for _,p in named if p.grad is not None)
            if not math.isfinite(grad_norm) or grad_norm <= 0:raise RuntimeError('no nonzero finite LoRA gradient')
            optimizer.step(); torch.cuda.synchronize()
            losses.append(float(loss.detach().item()));gradients.append(grad_norm)
        result['training_seconds']=round(time.monotonic()-start,3)
        result['losses']=losses;result['gradient_norm_sums']=gradients
        result['changed_lora_tensors']=sum(not torch.equal(initial[n],p.detach().cpu()) for n,p in named)
        if result['changed_lora_tensors']==0:raise RuntimeError('optimizer did not update LoRA weights')
        result['training_peak_allocated_mib']=round(torch.cuda.max_memory_allocated()/2**20,2)
        result['training_peak_reserved_mib']=round(torch.cuda.max_memory_reserved()/2**20,2)
        adapter_path=args.output_dir/'adapter'
        model.save_pretrained(adapter_path,safe_serialization=True)
        tokenizer.save_pretrained(args.output_dir/'tokenizer')
        model.eval()
        inputs={k:v for k,v in prepared[0].items() if k!='labels'}
        with torch.inference_mode():
            before=model(**inputs,use_cache=False,logits_to_keep=1).logits.detach().float().cpu()
        del output, loss, optimizer, initial, named, model
        gc.collect();torch.cuda.empty_cache()
        base=load_base().eval()
        reloaded=PeftModel.from_pretrained(base,adapter_path,is_trainable=False).eval()
        with torch.inference_mode():
            after=reloaded(**inputs,use_cache=False,logits_to_keep=1).logits.detach().float().cpu()
        diff=(before-after).abs()
        result['reload_max_abs_logit_diff']=float(diff.max().item())
        result['reload_mean_abs_logit_diff']=float(diff.mean().item())
        result['reload_argmax_match']=bool(before.argmax().item()==after.argmax().item())
        result['adapter_bytes']=sum(x.stat().st_size for x in adapter_path.iterdir() if x.is_file())
        if not torch.allclose(before,after,atol=1e-3,rtol=1e-3):raise RuntimeError('reloaded logits mismatch')
        result['success']=True
    except Exception as exc:
        result['error']=str(exc);result['traceback']=traceback.format_exc()
        try:
            result['failure_peak_allocated_mib']=round(torch.cuda.max_memory_allocated()/2**20,2)
            result['failure_peak_reserved_mib']=round(torch.cuda.max_memory_reserved()/2**20,2)
        except Exception:pass
    (args.output_dir/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='trainable_parameter_names'},ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':
    sys.exit(main())
