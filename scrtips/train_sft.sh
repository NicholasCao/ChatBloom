torchrun --standalone --nproc_per_node=4 train_sft.py \
    --pretrain bigscience/bloom-1b7 \
    --model bloom \
    --strategy colossalai_zero2 \
    --data_path data/sft_data.json \
    --save_path  outputs/bloom-1b7-sft \
    --batch_size 4 \
    --accumulation_steps 8 \
    --lr 2e-5 \
    --max_len 768 \
    --max_epochs 3
