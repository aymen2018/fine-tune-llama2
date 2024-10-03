# -*- coding: utf-8 -*-
"""llma2-fune-tuning.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1PUQEXSgGMR0s9c1vn5cc8AEa63t5ZTq1
"""

!nvidia-smi -L

import torch

# Check if a GPU is available
if torch.cuda.is_available():
    # Get the number of available GPUs
    num_gpus = torch.cuda.device_count()
    print(f"Number of available GPUs: {num_gpus}")
    # Get the name of the GPU(s)
    for i in range(num_gpus):
        print(f"GPU {i}: {torch.cuda.get_device_name(i)}")
else:
    print("No GPU available.")

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# %pip install accelerate peft bitsandbytes transformers trl

# @title Titre par défaut
import argparse
import bitsandbytes as bnb
from datasets import load_dataset
from functools import partial
import os
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training, AutoPeftModelForCausalLM
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed, Trainer, TrainingArguments, BitsAndBytesConfig, \
    DataCollatorForLanguageModeling, Trainer, TrainingArguments
from datasets import load_dataset

"""**Load Model**"""

def load_model(model_name, bnb_config):
    n_gpus = torch.cuda.device_count()
    max_memory = f'{40960}MB'

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="auto", # dispatch efficiently the model on the available ressources
        max_memory = {i: max_memory for i in range(n_gpus)},
    )
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_auth_token=True)

    # Needed for LLaMA tokenizer
    tokenizer.pad_token = tokenizer.eos_token

    return model, tokenizer

"""**Load Dataset**"""

# Load the databricks dataset from Hugging Face
from datasets import load_dataset
dataset = load_dataset("databricks/databricks-dolly-15k", split="train")

print(f'Number of prompts: {len(dataset)}')
print(f'Column names are: {dataset.column_names}')

def create_prompt_formats(sample):
    """
    Format various fields of the sample ('instruction', 'context', 'response')
    Then concatenate them using two newline characters
    :param sample: Sample dictionnary
    """

    INTRO_BLURB = "Below is an instruction that describes a task. Write a response that appropriately completes the request."
    INSTRUCTION_KEY = "### Instruction:"
    INPUT_KEY = "Input:"
    RESPONSE_KEY = "### Response:"
    END_KEY = "### End"

    blurb = f"{INTRO_BLURB}"
    instruction = f"{INSTRUCTION_KEY}\n{sample['instruction']}"
    input_context = f"{INPUT_KEY}\n{sample['context']}" if sample["context"] else None
    response = f"{RESPONSE_KEY}\n{sample['response']}"
    end = f"{END_KEY}"

    parts = [part for part in [blurb, instruction, input_context, response, end] if part]

    formatted_prompt = "\n\n".join(parts)

    sample["text"] = formatted_prompt

    return sample

# SOURCE https://github.com/databrickslabs/dolly/blob/master/training/trainer.py
def get_max_length(model):
    conf = model.config
    max_length = None
    for length_setting in ["n_positions", "max_position_embeddings", "seq_length"]:
        max_length = getattr(model.config, length_setting, None)
        if max_length:
            print(f"Found max lenth: {max_length}")
            break
    if not max_length:
        max_length = 1024
        print(f"Using default max length: {max_length}")
    return max_length


def preprocess_batch(batch, tokenizer, max_length):
    """
    Tokenizing a batch
    """
    return tokenizer(
        batch["text"],
        max_length=max_length,
        truncation=True,
    )


# SOURCE https://github.com/databrickslabs/dolly/blob/master/training/trainer.py
def preprocess_dataset(tokenizer: AutoTokenizer, max_length: int, seed, dataset: str):
    """Format & tokenize it so it is ready for training
    :param tokenizer (AutoTokenizer): Model Tokenizer
    :param max_length (int): Maximum number of tokens to emit from tokenizer
    """

    # Add prompt to each sample
    print("Preprocessing dataset...")
    dataset = dataset.map(create_prompt_formats)#, batched=True)

    # Apply preprocessing to each batch of the dataset & and remove 'instruction', 'context', 'response', 'category' fields
    _preprocessing_function = partial(preprocess_batch, max_length=max_length, tokenizer=tokenizer)
    dataset = dataset.map(
        _preprocessing_function,
        batched=True,
        remove_columns=["instruction", "context", "response", "text", "category"],
    )

    # Filter out samples that have input_ids exceeding max_length
    dataset = dataset.filter(lambda sample: len(sample["input_ids"]) < max_length)

    # Shuffle dataset
    dataset = dataset.shuffle(seed=seed)

    return dataset

def create_bnb_config():
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    return bnb_config

def create_peft_config(modules):
    """
    Create Parameter-Efficient Fine-Tuning config for your model
    :param modules: Names of the modules to apply Lora to
    """
    config = LoraConfig(
        r=16,  # dimension of the updated matrices
        lora_alpha=64,  # parameter for scaling
        target_modules=modules,
        lora_dropout=0.1,  # dropout probability for layers
        bias="none",
        task_type="CAUSAL_LM",
    )

    return config

# SOURCE https://github.com/artidoro/qlora/blob/main/qlora.py
def find_all_linear_names(model):
    cls = bnb.nn.Linear4bit #if args.bits == 4 else (bnb.nn.Linear8bitLt if args.bits == 8 else torch.nn.Linear)
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if 'lm_head' in lora_module_names:  # needed for 16-bit
        lora_module_names.remove('lm_head')
    return list(lora_module_names)

def print_trainable_parameters(model, use_4bit=False):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        num_params = param.numel()
        # if using DS Zero 3 and the weights are initialized empty
        if num_params == 0 and hasattr(param, "ds_numel"):
            num_params = param.ds_numel

        all_param += num_params
        if param.requires_grad:
            trainable_params += num_params
    if use_4bit:
        trainable_params /= 2
    print(
        f"all params: {all_param:,d} || trainable params: {trainable_params:,d} || trainable%: {100 * trainable_params / all_param}"
    )

# Load model from HF with user's token and with bitsandbytes config
from huggingface_hub.hf_api import HfFolder
HfFolder.save_token("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")

model_name = "meta-llama/Llama-2-7b-hf"

bnb_config = create_bnb_config()

model, tokenizer = load_model(model_name, bnb_config)

## call pipeline
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, TrainingArguments, pipeline, logging

"""## call pipeline"""

# Test the model
#logging.set_verbosity(logging.CRITICAL)
prompt = "Who is Leonardo Da Vinci?"
pipe = pipeline(task="text-generation", model=model, tokenizer=tokenizer, max_length=200)
result = pipe(f"<s>[INST] {prompt} [/INST]")
print(result[0]['generated_text'])

## Preprocess dataset
max_length = get_max_length(model)
seed = 42 ##shuffling
dataset = preprocess_dataset(tokenizer, max_length, seed, dataset)

def train(model, tokenizer, dataset, output_dir):
    # Apply preprocessing to the model to prepare it by
    # 1 - Enabling gradient checkpointing to reduce memory usage during fine-tuning
    model.gradient_checkpointing_enable()

    # 2 - Using the prepare_model_for_kbit_training method from PEFT
    model = prepare_model_for_kbit_training(model)

    # Get lora module names
    modules = find_all_linear_names(model)

    # Create PEFT config for these modules and wrap the model to PEFT
    peft_config = create_peft_config(modules)
    model = get_peft_model(model, peft_config)

    # Print information about the percentage of trainable parameters
    print_trainable_parameters(model)

    # Training parameters
    trainer = Trainer(
        model=model,
        train_dataset=dataset,
        args=TrainingArguments(
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            warmup_steps=2,
            max_steps=20,
            learning_rate=2e-4,
            fp16=True,
            logging_steps=1,
            output_dir="outputs",
            optim="paged_adamw_8bit",
        ),
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False)
    )

    model.config.use_cache = False  # re-enable for inference to speed up predictions for similar inputs

    ### SOURCE https://github.com/artidoro/qlora/blob/main/qlora.py
    # Verifying the datatypes before training

    dtypes = {}
    for _, p in model.named_parameters():
        dtype = p.dtype
        if dtype not in dtypes: dtypes[dtype] = 0
        dtypes[dtype] += p.numel()
    total = 0
    for k, v in dtypes.items(): total+= v
    for k, v in dtypes.items():
        print(k, v, v/total)

    do_train = True

    # Launch training
    print("Training...")

    if do_train:
        train_result = trainer.train()
        metrics = train_result.metrics
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
        print(metrics)


    # Saving model
    print("Saving last checkpoint of the model...")
    os.makedirs(output_dir, exist_ok=True)
    trainer.model.save_pretrained(output_dir)
    ##save on local also
    # output_dir_2 = r"C:\Users\IMINFO\Documents\llma\model"
    # trainer.model.save_pretrained(output_dir_2)

    # Free memory for merging weights
    del model
    del trainer
    torch.cuda.empty_cache()


output_dir = "results/llama2/final_checkpoint"
train(model, tokenizer, dataset, output_dir)

#model = AutoPeftModelForCausalLM.from_pretrained(output_dir, device_map="auto", torch_dtype=torch.bfloat16)
# Offload the model to the disk
from accelerate import disk_offload

# Load the model
model = AutoPeftModelForCausalLM.from_pretrained(output_dir)

disk_offload(model, device="auto", dtype=torch.bfloat16)

model = model.merge_and_unload()

output_merged_dir = "results/llama2/final_merged_checkpoint"
os.makedirs(output_merged_dir, exist_ok=True)
model.save_pretrained(output_merged_dir, safe_serialization=True)

# save tokenizer for easy inference
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.save_pretrained(output_merged_dir)

import os

# Specify the path to your saved model directory
model_directory = 'results/llama2/final_checkpoint'

# List the files in the directory
model_files = os.listdir(model_directory)

# Initialize a variable to store the total size
total_size = 0

# Iterate through the files and calculate the total size
for file_name in model_files:
    file_path = os.path.join(model_directory, file_name)
    if os.path.isfile(file_path):
        total_size += os.path.getsize(file_path)

# Convert the total size to a human-readable format (e.g., MB or GB)
def convert_bytes_to_readable(size_bytes):
    size = size_bytes
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.2f} {unit}"

# Print the total size
print(f"Total model size: {convert_bytes_to_readable(total_size)}")

import zipfile
import os
# Define the directory to compress
directory_to_compress = 'results/llama2/final_merged_checkpoint'
# Define the ZIP file name
zip_file_name = 'compressed_directory.zip'
# Create a ZIP archive and add all files and subdirectories
with zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
    for root, _, files in os.walk(directory_to_compress):
        for file in files:
            file_path = os.path.join(root, file)
            arcname = os.path.relpath(file_path, directory_to_compress)
            zipf.write(file_path, arcname=arcname)
# Move the ZIP archive to the desired location (optional)
# You can skip this step if you want to keep the ZIP file in the current directory
#os.rename(zip_file_name, f'/path/to/destination/{zip_file_name}')

from google.colab import files
directory_to_compress ='compressed_directory.zip'
# Provide the file path of the ZIP archive
files.download(f'{directory_to_compress}')

import shutil
from google.colab import files

# Zip the Colab directory
shutil.make_archive(r"C:\Users\IMINFO\Documents\llma\model", 'zip', r"C:\Users\IMINFO\Documents\llma\model")

# Download the ZIP archive to your local PC
files.download(r"C:\Users\IMINFO\Documents\llma\model")

pip install boto3
# pip install --upgrade boto3 urllib3
# pip list
# pip install boto3
# pip install --upgrade boto3 botocore
# pip install boto3==1.0.0
# !python -m pip uninstall boto3 botocore
# !python3 -m pip install boto3
# !pip install boto3==1.28.39 botocore==1.31.39
# !pip install boto3==1.15.3
# import boto3
# pip freeze
# !openssl version

import os

# Define your AWS credentials and region
aws_access_key_id = 'your-access-key-id'
aws_secret_access_key = 'your-secret-access-key'
aws_region = 'your-aws-region'

# Initialize the S3 client
s3 = boto3.client('s3', aws_access_key_id=aws_access_key_id,
                  aws_secret_access_key=aws_secret_access_key, region_name=aws_region)

# Specify the S3 bucket and key (object key)
s3_bucket = 'your-s3-bucket-name'
s3_key = 'your-model-folder/model.tar.gz'  # Define the S3 key for your model

# Specify the local path to your model directory
local_model_directory = '/path/to/your/local/model/directory'

# Walk through the local directory and upload each file to S3
for root, dirs, files in os.walk(local_model_directory):
    for file in files:
        local_file_path = os.path.join(root, file)
        s3_object_key = os.path.relpath(local_file_path, local_model_directory)
        s3.upload_file(local_file_path, s3_bucket, os.path.join(s3_key, s3_object_key))

print(f"Model uploaded to S3: s3://{s3_bucket}/{s3_key}")