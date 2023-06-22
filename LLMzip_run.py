# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from typing import Tuple
import os
import sys
import torch
import fire
import time
import json
import csv
import numpy as np

from pathlib import Path

from fairscale.nn.model_parallel.initialize import initialize_model_parallel

from llama import ModelArgs, Transformer, Tokenizer, LLMzip_encode, LLMzip_decode


### Command to run
# torchrun --nproc_per_node 1 LLMzip_run.py --ckpt_dir weights/7B --tokenizer_path weights/tokenizer.model 
# --win_len 511 --text_file *.txt --compression_folder LLMzip_compression   > Log_files/text8_ent1.txt 2>&1


def setup_model_parallel() -> Tuple[int, int]:
    local_rank = int(os.environ.get("LOCAL_RANK", -1))
    world_size = int(os.environ.get("WORLD_SIZE", -1))
    print("Local Rank : ",local_rank,", World Size : ",world_size)

    torch.distributed.init_process_group("nccl")
    initialize_model_parallel(world_size)
    torch.cuda.set_device(local_rank)

    # seed must be the same in all processes
    torch.manual_seed(1)
    return local_rank, world_size


def load(
    ckpt_dir: str,
    tokenizer_path: str,
    local_rank: int,
    world_size: int,
    max_seq_len: int,
    max_batch_size: int,
    return_decoder = False,
):
    start_time = time.time()
    
    checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
    assert world_size == len(
        checkpoints
    ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"
    ckpt_path = checkpoints[local_rank]
    print("Loading")
    checkpoint = torch.load(ckpt_path, map_location="cpu")
    with open(Path(ckpt_dir) / "params.json", "r") as f:
        params = json.loads(f.read())

    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len, max_batch_size=max_batch_size, **params
    )
    tokenizer = Tokenizer(model_path=tokenizer_path)
    model_args.vocab_size = tokenizer.n_words
    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    model = Transformer(model_args)
    torch.set_default_tensor_type(torch.FloatTensor)
    model.load_state_dict(checkpoint, strict=False)
    if return_decoder:
        Encoder = LLMzip_encode(model, tokenizer)
        Decoder = LLMzip_decode(model, tokenizer)
        print(f"Loaded in {time.time() - start_time:.2f} seconds")
        return Encoder,Decoder
    else:
        Encoder = LLMzip_encode(model, tokenizer)
        print(f"Loaded in {time.time() - start_time:.2f} seconds")
        return Encoder

def verify_text(compressed_file_name,decompressed_file_name,alg):
    with open(compressed_file_name+'_encoded_text.txt','r') as txt_enc:
        text_encoded = txt_enc.read()
    with open(decompressed_file_name+'_decoded_text.txt','r') as txt_enc:
        text_decoded = txt_enc.read()
        
    if text_encoded == text_decoded:
        print(f'Successful decoding using {alg}')
    else:
        print("********!!!!! Error !!!!!*********")
        print("***********Encoded Text************")
        print(text_encoded)
        print("***********Decoded Text************")
        print(text_decoded)

def main(
    ckpt_dir: str,
    tokenizer_path: str,
    win_len: int,
    text_file, 
    compression_folder,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    compression_alg: str = 'ArithmeticCoding',
    decode: int = 1,
    batched_encode = False,

):
    start_time_main = time.time()
    local_rank, world_size = setup_model_parallel()
    if local_rank > 0:
        sys.stdout = open(os.devnull, "w")
    
    decode = decode==1  # Convert to Bool
    
    
    
    if decode:
        Encoder,Decoder = load( ckpt_dir, tokenizer_path, local_rank, world_size, max_seq_len, max_batch_size,decode)
        batched_encode = False 
    else:
        Encoder = load( ckpt_dir, tokenizer_path, local_rank, world_size, max_seq_len, max_batch_size)

        # Use only for faster encoding (theoretical entropy computations)
        # batched_encode = True  # Batched encode does not decode correctly
    
    
    with open(text_file,'r') as f_in:
        text_input = f_in.read()

    tokens_full = np.array(Encoder.tokenizer.encode(text_input,bos=False,eos=False))

    
    # If the same tokens need to be encoded for any win_len (This has been used for our work)
    # tokens_full = np.array(Encoder.tokenizer.encode(text_input,bos=False,eos=False))[511-win_len:]
    
    total_length = tokens_full.size-win_len

    os.makedirs(compression_folder,exist_ok=True)
    
    compressed_file_name = compression_folder + f'/LLMzip_{win_len}'

    Encoder.encode_from_tokens(win_len,compression_alg,compressed_file_name,tokens_full=tokens_full,batched_encode=batched_encode)
    
    if decode:
        starter_tokens = tokens_full[:win_len]
        
        if (compression_alg == 'ArithmeticCoding')or(compression_alg =='both'): 
            compressed_file_name_full = compressed_file_name+'_AC.txt'
            decompressed_file_name = compressed_file_name+'_AC'
            
            Decoder.decode_AC(win_len,starter_tokens,total_length, compressed_file_name_full, decompressed_file_name)
            verify_text(compressed_file_name,decompressed_file_name,'ArithmeticCoding')
            
        if (compression_alg == 'RanksZip')or(compression_alg =='both'): 
            compressed_file_name_full = compressed_file_name+'_RZ.txt'
            decompressed_file_name = compressed_file_name+'_RZ'
            Decoder.decode_ranks(win_len,starter_tokens, compressed_file_name_full, decompressed_file_name)
            verify_text(compressed_file_name,decompressed_file_name,'RanksZip')
                    

    print(f"Completed in {time.time() - start_time_main:.2f} seconds")


if __name__ == "__main__":
    fire.Fire(main)
