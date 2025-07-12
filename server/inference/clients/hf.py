import json
import time
import logging
import torch
from typing import Any, Optional, AsyncGenerator, List, Dict
from transformers import AutoModelForCausalLM, AutoTokenizer

from ..base_llm_client import BaseLLMClient
from ..llm_client_common import LLMClientCommon

class HuggingFaceClient(BaseLLMClient, LLMClientCommon):
    """LLM client implementation using Hugging Face Transformers."""

    def __init__(self, config: Dict[str, Any], retriever: Any = None,
                 reranker_service: Any = None, prompt_service: Any = None, no_results_message: str = ""):
        # Initialize base classes properly
        BaseLLMClient.__init__(self, config, retriever, reranker_service, prompt_service, no_results_message)
        LLMClientCommon.__init__(self)  # Initialize the common client

        hf_cfg = config.get("inference", {}).get("huggingface", {})
        self.model_name = hf_cfg.get("model_name", "gpt2")
        self.device = hf_cfg.get("device", "cuda" if torch.cuda.is_available() else "cpu")
        self.max_length = hf_cfg.get("max_length", 1024)
        self.temperature = hf_cfg.get("temperature", 0.7)
        self.top_p = hf_cfg.get("top_p", 0.9)
        self.stream = hf_cfg.get("stream", False)
        self.verbose = hf_cfg.get('verbose', config.get('general', {}).get('verbose', False))

        self.model = None
        self.tokenizer = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"Initializing HuggingFace client with model: {self.model_name}")

    async def initialize(self) -> None:
        """Load model and tokenizer."""
        try:
            if self.model is None or self.tokenizer is None:
                self.logger.info(f"Loading tokenizer for model: {self.model_name}")
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                
                self.logger.info(f"Loading model: {self.model_name} to device: {self.device}")
                self.model = AutoModelForCausalLM.from_pretrained(self.model_name).to(self.device)
                self.model.eval()
                
                # Ensure tokenizer has padding token
                if self.tokenizer.pad_token is None:
                    self.tokenizer.pad_token = self.tokenizer.eos_token
                
                self.logger.info(f"HuggingFace model loaded successfully: {self.model_name} on {self.device}")
        except Exception as e:
            self.logger.error(f"Failed to initialize HuggingFace model: {str(e)}")
            raise

    async def close(self) -> None:
        """Cleanup model from memory."""
        try:
            if self.model is not None:
                del self.model
                self.model = None
            if self.tokenizer is not None:
                del self.tokenizer
                self.tokenizer = None
            torch.cuda.empty_cache()
            self.logger.info("HuggingFace model unloaded successfully")
        except Exception as e:
            self.logger.error(f"Error during model cleanup: {str(e)}")
            raise

    async def verify_connection(self) -> bool:
        """Quick check: load model briefly."""
        try:
            await self.initialize()
            return True
        except Exception as e:
            self.logger.error(f"HuggingFace verify_connection failed: {str(e)}")
            return False

    async def generate_response(
        self,
        message: str,
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[dict, None]:
        """Generate response using Hugging Face model."""
        try:
            retrieved_docs = await self._retrieve_and_rerank_docs(message, adapter_name)
            system_prompt = await self._get_system_prompt(system_prompt_id)
            context = self._format_context(retrieved_docs)

            # If no context was found, return the default no-results message
            if context is None:
                no_results_message = self.config.get('messages', {}).get('no_results_response', 
                    "I'm sorry, but I don't have any specific information about that topic in my knowledge base.")
                yield {
                    "response": no_results_message,
                    "sources": [],
                    "tokens": 0,
                    "token_usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    },
                    "processing_time": 0
                }
                return

            await self.initialize()

            # Build the prompt with context messages
            prompt_parts = []
            
            # Add system prompt if provided
            if system_prompt:
                prompt_parts.append(f"System: {system_prompt}")
            
            # Add context information
            if context:
                prompt_parts.append(f"Context information:\n{context}")
            
            # Add context messages if provided
            if context_messages:
                for msg in context_messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role == "user":
                        prompt_parts.append(f"User: {content}")
                    elif role == "assistant":
                        prompt_parts.append(f"Assistant: {content}")
            
            # Add the current message
            prompt_parts.append(f"User: {message}")
            prompt_parts.append("Assistant:")
            
            # Join all parts with newlines
            prompt = "\n".join(prompt_parts)

            inputs = self.tokenizer(prompt, return_tensors="pt", padding=True, truncation=True).to(self.device)
            start = time.time()
            
            with torch.no_grad():
                outputs = self.model.generate(
                    **inputs,
                    max_length=inputs["input_ids"].shape[1] + self.max_length,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    do_sample=True,
                    pad_token_id=self.tokenizer.pad_token_id,
                    eos_token_id=self.tokenizer.eos_token_id
                )
            
            elapsed = time.time() - start

            generated = outputs[0][inputs["input_ids"].shape[1]:]
            text = self.tokenizer.decode(generated, skip_special_tokens=True)

            sources = self._format_sources(retrieved_docs)
            if not isinstance(sources, list):
                self.logger.warning(f"'_format_sources' returned type {type(sources).__name__} (value: {str(sources)[:100]}) for sources, defaulting to empty list.")
                sources = []

            # Wrap response with security checking
            response_dict = {
                "response": text.strip(),
                "sources": sources,
                "tokens": len(generated),
                "token_usage": {
                    "prompt_tokens": inputs["input_ids"].shape[1],
                    "completion_tokens": len(generated),
                    "total_tokens": inputs["input_ids"].shape[1] + len(generated)
                },
                "processing_time": elapsed
            }
            
            secured_response = await self._secure_response(response_dict)
            yield secured_response
        except Exception as e:
            self.logger.error(f"Error generating response: {str(e)}")
            yield {
                "error": str(e),
                "response": "",
                "sources": [],
                "tokens": 0,
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                },
                "processing_time": 0
            }

    async def generate_response_stream(
        self,
        message: str,
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[str, None]:
        # Wrap the entire streaming response with security checking
        async for chunk in self._secure_response_stream(
            self._generate_response_stream_internal(message, adapter_name, system_prompt_id, context_messages)
        ):
            yield chunk
    
    async def _generate_response_stream_internal(
        self,
        message: str,
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[str, None]:
        """Stream response using Hugging Face model."""
        try:
            retrieved_docs = await self._retrieve_and_rerank_docs(message, adapter_name)
            system_prompt = await self._get_system_prompt(system_prompt_id)
            context = self._format_context(retrieved_docs)

            # If no context was found, return the default no-results message
            if context is None:
                no_results_message = self.config.get('messages', {}).get('no_results_response', 
                    "I'm sorry, but I don't have any specific information about that topic in my knowledge base.")
                yield json.dumps({
                    "response": no_results_message,
                    "sources": [],
                    "done": True
                })
                return

            await self.initialize()
            async for result in self.generate_response(message, adapter_name, system_prompt_id, context_messages):
                if "error" in result:
                    yield json.dumps({
                        "error": result["error"],
                        "done": True
                    })
                    return
                
                yield json.dumps({
                    "response": result["response"],
                    "sources": result["sources"],
                    "done": True,
                    "tokens": result["tokens"],
                    "token_usage": result["token_usage"],
                    "processing_time": result["processing_time"]
                })
        except Exception as e:
            self.logger.error(f"Error in generate_response_stream: {str(e)}")
            yield json.dumps({
                "error": str(e),
                "done": True
            })
