import math
import torch

from collections import namedtuple
from backend.text_processing import parsing, emphasis
from backend.text_processing.textual_inversion import EmbeddingDatabase
from backend import memory_management


PromptChunkFix = namedtuple('PromptChunkFix', ['offset', 'embedding'])


class PromptChunk:
    def __init__(self):
        self.tokens = []
        self.multipliers = []


class T5TextProcessingEngine:
    def __init__(self, text_encoder, tokenizer, emphasis_name="Original", min_length=256):
        super().__init__()

        self.text_encoder = text_encoder.transformer
        self.tokenizer = tokenizer

        self.emphasis = emphasis.get_current_option(emphasis_name)()
        self.min_length = min_length
        self.id_end = self.tokenizer('')["input_ids"][0]

        vocab = self.tokenizer.get_vocab()

        self.comma_token = vocab.get(',</w>', None)

        self.token_mults = {}

        tokens_with_parens = [(k, v) for k, v in vocab.items() if '(' in k or ')' in k or '[' in k or ']' in k]
        for text, ident in tokens_with_parens:
            mult = 1.0
            for c in text:
                if c == '[':
                    mult /= 1.1
                if c == ']':
                    mult *= 1.1
                if c == '(':
                    mult *= 1.1
                if c == ')':
                    mult /= 1.1

            if mult != 1.0:
                self.token_mults[ident] = mult

    def get_target_prompt_token_count(self, token_count):
        return token_count

    def tokenize(self, texts):
        tokenized = self.tokenizer(texts, truncation=False, add_special_tokens=False)["input_ids"]
        return tokenized

    def encode_with_transformers(self, tokens):
        tokens = tokens.to(memory_management.get_torch_device())
        device = memory_management.get_torch_device()
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32
        self.text_encoder.shared.to(device=device, dtype=dtype)

        z = self.text_encoder(
            input_ids=tokens,
        )

        return z

    def tokenize_line(self, line):
        parsed = parsing.parse_prompt_attention(line)

        tokenized = self.tokenize([text for text, _ in parsed])

        chunks = []
        chunk = PromptChunk()
        token_count = 0

        def next_chunk():
            nonlocal token_count
            nonlocal chunk

            token_count += len(chunk.tokens)
            to_add = self.min_length - len(chunk.tokens) - 1
            if to_add > 0:
                chunk.tokens += [self.id_end] * to_add
                chunk.multipliers += [1.0] * to_add

            chunk.tokens = chunk.tokens + [self.id_end]
            chunk.multipliers = chunk.multipliers + [1.0]

            chunks.append(chunk)
            chunk = PromptChunk()

        for tokens, (text, weight) in zip(tokenized, parsed):
            if text == 'BREAK' and weight == -1:
                next_chunk()
                continue

            position = 0
            while position < len(tokens):
                token = tokens[position]
                chunk.tokens.append(token)
                chunk.multipliers.append(weight)
                position += 1

        if chunk.tokens or not chunks:
            next_chunk()

        return chunks, token_count

    def process_texts(self, texts):
        token_count = 0

        cache = {}
        batch_chunks = []
        for line in texts:
            if line in cache:
                chunks = cache[line]
            else:
                chunks, current_token_count = self.tokenize_line(line)
                token_count = max(current_token_count, token_count)

                cache[line] = chunks

            batch_chunks.append(chunks)

        return batch_chunks, token_count

    def __call__(self, texts):
        batch_chunks, token_count = self.process_texts(texts)
        chunk_count = max([len(x) for x in batch_chunks])

        zs = []

        for i in range(chunk_count):
            batch_chunk = [chunks[i] for chunks in batch_chunks]
            tokens = [x.tokens for x in batch_chunk]
            multipliers = [x.multipliers for x in batch_chunk]
            z = self.process_tokens(tokens, multipliers)
            zs.append(z)

        return torch.hstack(zs)

    def process_tokens(self, remade_batch_tokens, batch_multipliers):
        tokens = torch.asarray(remade_batch_tokens)

        z = self.encode_with_transformers(tokens)

        self.emphasis.tokens = remade_batch_tokens
        self.emphasis.multipliers = torch.asarray(batch_multipliers).to(z)
        self.emphasis.z = z
        self.emphasis.after_transformers()
        z = self.emphasis.z

        return z
