import os
import torch
from typing import Optional
from dataclasses import dataclass, field

from transformers import (
    AutoConfig,
    AutoModelForMultipleChoice,
    AutoModelForCausalLM,
    AutoTokenizer,
    HfArgumentParser,
    T5ForConditionalGeneration,
    Trainer,
    TrainingArguments,
)
from transformers.file_utils import PaddingStrategy
from transformers.tokenization_utils_base import TruncationStrategy
from transformers.data.data_collator import default_data_collator

import lrqa.tasks as tasks
from lrqa.utils.hf_utils import parse_args, last_checkpoint_handling
from lrqa.utils.io_utils import write_json
from lrqa.utils.model_tweaks import adjust_tokenizer
from lrqa.utils.tokenizer_utils import get_tokenized_dataset
from lrqa.trainers import GenerationTrainer
from typing import Dict, List


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    model_mode: str = field(
        metadata={"help": "{mc,generation,encoder-decoder}"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    max_seq_length: int = field(
        default=256,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    padding_strategy: PaddingStrategy = field(
        default="max_length",
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    parallelize: bool = field(
        default=False,
        metadata={
            "help": "Whether to parallelize the model."
        }
    )
    truncation_strategy: TruncationStrategy = field(
        default="only_first",
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )

    def __post_init__(self):
        self.padding_strategy = PaddingStrategy(self.padding_strategy)
        self.truncation_strategy = TruncationStrategy(self.truncation_strategy)


def main():
    model_args, task_args, training_args = parse_args(HfArgumentParser((
        ModelArguments,
        tasks.TaskArguments,
        TrainingArguments,
    )))
    config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
        revision=model_args.model_revision,
    )
    adjust_tokenizer(tokenizer)
    if model_args.model_mode == "mc":
        model = AutoModelForMultipleChoice.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
        )
    elif model_args.model_mode == "generation":
        model = AutoModelForCausalLM.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
        )
    elif model_args.model_mode == "encoder-decoder":
        model = T5ForConditionalGeneration.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
        )
    else:
        raise KeyError(model_args.model_mode)
    if model_args.parallelize:
        model.parallelize()
    task = tasks.get_task(task_args=task_args)
    dataset_dict = task.get_datasets()
    tokenized_dataset_dict = get_tokenized_dataset(
        task=task,
        dataset_dict=dataset_dict,
        tokenizer=tokenizer,
        max_seq_length=model_args.max_seq_length,
        padding_strategy=model_args.padding_strategy,
        truncation_strategy=model_args.truncation_strategy,
    )
    if model_args.model_mode == "mc":
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_dataset_dict.get("train"),
            eval_dataset=tokenized_dataset_dict.get("validation"),
            compute_metrics=task.compute_metrics,
            tokenizer=tokenizer,
        )
    elif model_args.model_mode == "generation":
        training_args.remove_unused_columns = False
        trainer = GenerationTrainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_dataset_dict.get("train"),
            eval_dataset=tokenized_dataset_dict.get("validation"),
            compute_metrics=task.compute_metrics,
            tokenizer=tokenizer,
            data_collator=default_data_collator,
        )
    elif model_args.model_mode == "encoder-decoder":
        training_args.remove_unused_columns = False
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_dataset_dict.get("train"),
            eval_dataset=tokenized_dataset_dict.get("validation"),
            # compute_metrics=task.compute_metrics,
            prediction_loss_only=True,
            tokenizer=tokenizer,
        )
    else:
        raise KeyError(model_args.model_mode)

    checkpoint = last_checkpoint_handling(training_args=training_args, model_args=model_args)
    if training_args.do_train:
        train_result = trainer.train(resume_from_checkpoint=checkpoint)

        trainer.save_model(output_dir=os.path.join(training_args.output_dir, "checkpoint-last"))
        # noinspection PyArgumentList
        trainer.log_metrics("train", train_result.metrics)
        # noinspection PyArgumentList
        trainer.save_metrics("train", train_result.metrics)
        # noinspection PyArgumentList
        trainer.save_state()

    if training_args.do_eval:
        validation_metrics = trainer.evaluate(eval_dataset=tokenized_dataset_dict["validation"])
        write_json(validation_metrics, os.path.join(training_args.output_dir, "val_metrics.json"))

    if training_args.do_predict:
        predictions = trainer.predict(test_dataset=tokenized_dataset_dict["test"]).predictions
        torch.save(predictions, os.path.join(training_args.output_dir, "test_predictions.p"))


if __name__ == "__main__":
    main()
