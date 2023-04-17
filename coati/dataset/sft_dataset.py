#    Copyright 2023 Rohan Taori, Ishaan Gulrajani, Tianyi Zhang, Yann Dubois, Xuechen Li
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import os
import copy
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Sequence, Optional, List

import torch
import torch.distributed as dist
import transformers
from torch.utils.data import Dataset
from tqdm import tqdm
from datasets import load_dataset

from colossalai.logging import get_dist_logger

from .utils import is_rank_0, jload

logger = get_dist_logger()

IGNORE_INDEX = -100
PROMPT_DICT = {
    "prompt_input":
        ("Below is an instruction that describes a task, paired with an input that provides further context. "
         "Write a response that appropriately completes the request.\n\n"
         "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"),
    # "prompt_no_input": ("Below is an instruction that describes a task. "
    #                     "Write a response that appropriately completes the request.\n\n"
    #                     "### Instruction:\n{instruction}\n\n### Response:"),
    "prompt_no_input": ("{instruction}\n\n"),
}

def pCLUE_preprocess(data_path: str, tokenizer: Callable, max_length: int = 512, max_datasets_size: int = 600000):
    dataset = load_dataset(data_path, split='train')
    
    input_ids = []
    input_lens = []
    labels = []
    for idx, data in enumerate(tqdm(dataset, disable=not is_rank_0(), mininterval=3, desc=f'preprocssing {data_path}')):
        
        input_text = data['input'] + data['target'] + tokenizer.eos_token
        
        inputs = tokenizer(input_text,
                           max_length=max_length,
                           padding="longest",
                           truncation=True,
                           return_tensors="pt")
        if len(inputs['input_ids'][0]) < 20 or len(inputs['input_ids'][0]) >= max_length - 1:
            continue
        
        input_len = len(tokenizer.tokenize(data['input']))
        
        input_ids.append(inputs['input_ids'][0])
        input_lens.append(input_len)
        
        if max_datasets_size is not None and idx >= max_datasets_size:
            break
    
    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, input_lens):
        label[:source_len] = IGNORE_INDEX
    return input_ids, labels

def BELLE_preprocess(data_path: str, tokenizer: Callable, max_length: int = 512, max_datasets_size: int = 1000000, require_enter: bool = False):
    dataset = load_dataset(data_path, split='train')
    
    input_ids = []
    input_lens = []
    labels = []
    
    enter = '\n' if require_enter else ''
    for idx, data in enumerate(tqdm(dataset, disable=not is_rank_0(), mininterval=3, desc=f'preprocssing {data_path}')):
        
        input_text = data['instruction'] + enter + data['output'] + tokenizer.eos_token
        
        inputs = tokenizer(input_text,
                           max_length=max_length,
                           padding="longest",
                           truncation=True,
                           return_tensors="pt")
        if len(inputs['input_ids'][0]) < 20 or len(inputs['input_ids'][0]) >= max_length - 1:
            continue
        
        input_len = len(tokenizer.tokenize(data['instruction']))
        
        input_ids.append(inputs['input_ids'][0])
        input_lens.append(input_len)
        
        if max_datasets_size is not None and idx >= max_datasets_size:
            break

    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, input_lens):
        label[:source_len] = IGNORE_INDEX
    return input_ids, labels
        
class ITDataset(Dataset):
    """
    Dataset for instruction tuning

    Args:
        tokenizer: tokenizer for supervised model
        max_length: max length of input
    """

    def __init__(self, tokenizer: Callable, max_length: int = 512) -> None:
        super().__init__()
        self.input_ids = []
        self.labels = []
        
        # input_ids, labels = pCLUE_preprocess('wbbbbb/pclue', tokenizer, max_length)
        # self.input_ids += input_ids
        # self.labels += labels
        
        input_ids, labels = BELLE_preprocess('BelleGroup/generated_chat_0.4M', tokenizer, max_length, 200000)
        self.input_ids += input_ids
        self.labels += labels

        input_ids, labels = BELLE_preprocess('BelleGroup/train_2M_CN', tokenizer, max_length, 500000, require_enter=True)
        self.input_ids += input_ids
        self.labels += labels
        
        input_ids, labels = pCLUE_preprocess('wbbbbb/pclue', tokenizer, max_length, 300000)
        self.input_ids += input_ids
        self.labels += labels
                
    def __len__(self):
        length = len(self.input_ids)
        return length

    def __getitem__(self, idx):
        return dict(input_ids=self.input_ids[idx], labels=self.labels[idx])

def _tokenize_fn(strings: Sequence[str], tokenizer: transformers.PreTrainedTokenizer, max_length: int) -> Dict:
    """Tokenize a list of strings."""
    tokenized_list = [
        tokenizer(
            text,
            return_tensors="pt",
            padding="longest",
            max_length=max_length,
            truncation=True,
        ) for text in strings
    ]
    input_ids = labels = [tokenized.input_ids[0] for tokenized in tokenized_list]
    input_ids_lens = labels_lens = [
        tokenized.input_ids.ne(tokenizer.pad_token_id).sum().item() for tokenized in tokenized_list
    ]
    return dict(
        input_ids=input_ids,
        labels=labels,
        input_ids_lens=input_ids_lens,
        labels_lens=labels_lens,
    )


def preprocess(
    sources: Sequence[str],
    targets: Sequence[str],
    tokenizer: transformers.PreTrainedTokenizer,
    max_length: int,
) -> Dict:
    """Preprocess the data by tokenizing."""
    examples = [s + t for s, t in zip(sources, targets)]
    examples_tokenized, sources_tokenized = [_tokenize_fn(strings, tokenizer, max_length) for strings in (examples, sources)]
    input_ids = examples_tokenized["input_ids"]
    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, sources_tokenized["input_ids_lens"]):
        label[:source_len] = IGNORE_INDEX
    return dict(input_ids=input_ids, labels=labels)


def format_chat(dataset, input_key: str = 'instruction', output_key: str = 'output', is_chat: bool = False):
    inputs = []
    outputs = []

    if is_chat:
        for data in dataset:
            formated_input = data[input_key].replace(' Assistant:', '\nAssistant:')
            formated_input = formated_input.replace(r'Assistant:(?=\S+)', 'Assistant: ')
            formated_input = formated_input.replace(r'Human:(?=\S+)', 'Human: ')
            
            formated_output = data[output_key][1:] if data[output_key].startswith(' ') else data[output_key]
            inputs.append(formated_input)
            outputs.append(formated_output)
    else:
        for data in dataset:
            inputs.append(f'Human: {data[input_key].strip()}\nAssistant: ')
            outputs.append(data[output_key])
    return inputs, outputs
    

def chat_preprocess(inputs: List[str], outputs: List[str], tokenizer: Callable, max_length: int = 512, max_datasets_size: int = 1000000):
    input_ids = []
    input_lens = []
    for idx, (inp, oup) in enumerate(tqdm(zip(inputs, outputs), disable=not is_rank_0(), mininterval=3)):
        
        input_text = inp + oup + tokenizer.eos_token
        
        inputs = tokenizer(input_text,
                           max_length=max_length,
                           padding="longest",
                           truncation=True,
                           return_tensors="pt")
        if len(inputs['input_ids'][0]) < 20 or len(inputs['input_ids'][0]) >= max_length - 1:
            continue
        
        input_len = len(tokenizer.tokenize(inp))
        
        input_ids.append(inputs['input_ids'][0])
        input_lens.append(input_len)
        
        if max_datasets_size is not None and idx >= max_datasets_size:
            break

    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, input_lens):
        label[:source_len] = IGNORE_INDEX
    return input_ids, labels

class SFTDataset(Dataset):
    """
    Dataset for instruction tuning

    Args:
        tokenizer: tokenizer for supervised model
        data_path: InstructionWild data path
        max_length: max length of input
    """

    def __init__(self, tokenizer: Callable, max_length: int = 512, data_path: Optional[str] = None) -> None:
        super().__init__()
        self.input_ids = []
        self.labels = []
        
        
        belle_1M = load_dataset('BelleGroup/train_1M_CN', split='train')
        inputs, outputs = format_chat(belle_1M, "instruction", "output")
        input_ids, labels = chat_preprocess(inputs, outputs, tokenizer, max_length, max_datasets_size=100000)
        self.input_ids += input_ids
        self.labels += labels
        
        belle_mtchat = load_dataset('BelleGroup/multiturn_chat_0.8M', split='train')
        inputs, outputs = format_chat(belle_mtchat, "instruction", "output", is_chat=True)
        input_ids, labels = chat_preprocess(inputs, outputs, tokenizer, max_length, max_datasets_size=200000)
        self.input_ids += input_ids
        self.labels += labels
        
        if data_path is not None:
            list_data_dict = jload(os.path.join(data_path, 'instinwild_ch.json'))
            list_data_dict_en = jload(os.path.join(data_path, 'instinwild_en.json'))
        
            inputs, outputs = format_chat(list_data_dict, "instruction", "output")
            input_ids, labels = chat_preprocess(inputs, outputs, tokenizer, max_length, max_datasets_size=30000)
            self.input_ids += input_ids
            self.labels += labels
            
            inputs, outputs = format_chat(list_data_dict_en, "instruction", "output")
            input_ids, labels = chat_preprocess(inputs, outputs, tokenizer, max_length, max_datasets_size=30000)
            self.input_ids += input_ids
            self.labels += labels
                
    def __len__(self):
        length = len(self.input_ids)
        return length

    def __getitem__(self, idx):
        return dict(input_ids=self.input_ids[idx], labels=self.labels[idx])


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self, data_path: str, tokenizer: transformers.PreTrainedTokenizer, max_datasets_size: int = None, max_length: int = 512):
        super(SupervisedDataset, self).__init__()
        logger.info("Loading data...", ranks=[0])
        list_data_dict = jload(data_path)
        logger.info(f"Loaded {len(list_data_dict)} examples.", ranks=[0])

        if max_datasets_size is not None:
            logger.info(f"Limiting dataset to {max_datasets_size} examples.", ranks=[0])
            list_data_dict = list_data_dict[:max_datasets_size]

        logger.info("Formatting inputs...", ranks=[0])
        prompt_input, prompt_no_input = PROMPT_DICT["prompt_input"], PROMPT_DICT["prompt_no_input"]
        sources = [
            prompt_input.format_map(example) if example.get("input", "") != "" else prompt_no_input.format_map(example)
            for example in list_data_dict
        ]
        targets = [f"{example['output']}{tokenizer.eos_token}" for example in list_data_dict]

        logger.info("Tokenizing inputs... This may take some time...", ranks=[0])
        data_dict = preprocess(sources, targets, tokenizer, max_length)

        self.input_ids = data_dict["input_ids"]
        self.labels = data_dict["labels"]

    def __len__(self):
        return len(self.input_ids)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        return dict(input_ids=self.input_ids[i], labels=self.labels[i])


@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""

    tokenizer: transformers.PreTrainedTokenizer

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        input_ids, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "labels"))
        input_ids = torch.nn.utils.rnn.pad_sequence(input_ids,
                                                    batch_first=True,
                                                    padding_value=self.tokenizer.pad_token_id)
        labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=input_ids.ne(self.tokenizer.pad_token_id),
        )
