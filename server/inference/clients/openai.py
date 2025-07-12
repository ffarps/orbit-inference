"""
OpenAI client implementation for LLM inference.

This module provides an OpenAI-specific implementation of the BaseLLMClient interface.
"""

import json
import time
from typing import Dict, List, Any, Optional, AsyncGenerator
import aiohttp
import logging
import os

from ..base_llm_client import BaseLLMClient
from ..llm_client_common import LLMClientCommon

class OpenAIClient(BaseLLMClient, LLMClientCommon):
    """LLM client implementation for OpenAI."""
    
    def __init__(self, config: Dict[str, Any], retriever: Any = None,
                 reranker_service: Any = None, prompt_service: Any = None, no_results_message: str = ""):
        # Initialize base classes properly
        BaseLLMClient.__init__(self, config, retriever, reranker_service, prompt_service, no_results_message)
        LLMClientCommon.__init__(self)  # Initialize the common client
        
        # Get OpenAI specific configuration
        openai_config = config.get('inference', {}).get('openai', {})
        
        self.api_key = os.getenv("OPENAI_API_KEY", openai_config.get('api_key', ''))
        self.model = openai_config.get('model', 'gpt-4o')
        self.temperature = openai_config.get('temperature', 0.1)
        self.top_p = openai_config.get('top_p', 0.8)
        self.max_tokens = openai_config.get('max_tokens', 1024)
        self.stream = openai_config.get('stream', True)
        self.verbose = openai_config.get('verbose', config.get('general', {}).get('verbose', False))
        
        self.openai_client = None
        
    async def initialize(self) -> None:
        """Initialize the OpenAI client."""
        try:
            from openai import AsyncOpenAI
            
            # Initialize OpenAI client
            self.openai_client = AsyncOpenAI(api_key=self.api_key)
            
            self.logger.info(f"Initialized OpenAI client with model {self.model}")
        except ImportError:
            self.logger.error("openai package not installed or outdated. Please install with: pip install -U openai>=1.0.0")
            raise
        except Exception as e:
            self.logger.error(f"Error initializing OpenAI client: {str(e)}")
            raise
    
    async def close(self) -> None:
        """Clean up resources."""
        try:
            if self.openai_client and hasattr(self.openai_client, "close"):
                await self.openai_client.close()
                self.logger.info("Closed OpenAI client session")
        except Exception as e:
            self.logger.error(f"Error closing OpenAI client: {str(e)}")
        
        # Signal that cleanup is complete
        self.logger.info("OpenAI client resources released")
    
    async def verify_connection(self) -> bool:
        """
        Verify that the connection to OpenAI is working.
        
        Returns:
            True if connection is working, False otherwise
        """
        try:
            if not self.openai_client:
                await self.initialize()
            
            if self.verbose:
                self.logger.info("Testing OpenAI API connection")
                
            # Simple test request to verify connection
            response = await self.openai_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Ping"}
                ],
                max_completion_tokens=10
            )
            
            if self.verbose:
                self.logger.info("Successfully connected to OpenAI API")
                
            # If we get here, the connection is working
            return True
        except Exception as e:
            self.logger.error(f"Error connecting to OpenAI API: {str(e)}")
            return False
    
    async def generate_response(
        self, 
        message: str, 
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Generate a response for a chat message using OpenAI.
        
        Args:
            message: The user's message
            adapter_name: Name of the adapter to use for retrieval
            system_prompt_id: Optional ID of a system prompt to use
            context_messages: Optional list of previous conversation messages
            
        Returns:
            Dictionary containing response and metadata
        """
        try:
            if self.verbose:
                self.logger.info(f"Generating response for message: {message[:100]}...")
                            
            # Retrieve and rerank documents
            retrieved_docs = await self._retrieve_and_rerank_docs(message, adapter_name)
            
            # Get the system prompt
            system_prompt = await self._get_system_prompt(system_prompt_id)
            
            # Format the context from retrieved documents
            context = self._format_context(retrieved_docs)
            
            # If no context was found, return the default no-results message
            if context is None:
                no_results_message = self.config.get('messages', {}).get('no_results_response', 
                    "I'm sorry, but I don't have any specific information about that topic in my knowledge base.")
                return {
                    "response": no_results_message,
                    "sources": [],
                    "tokens": 0,
                    "processing_time": 0
                }
            
            # Initialize OpenAI client if not already initialized
            if not self.openai_client:
                await self.initialize()
            
            if self.verbose:
                self.logger.info(f"Calling OpenAI API with model: {self.model}")
                
            # Call the OpenAI API
            start_time = time.time()
            
            # Prepare messages for the API call
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add context messages if provided
            if context_messages:
                messages.extend(context_messages)
            
            # Add the current message with context
            messages.append({
                "role": "user", 
                "content": f"Context information:\n{context}\n\nUser Query: {message}"
            })
            
            # Prepare parameters based on model
            params = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": self.max_tokens
            }
            
            # Add temperature and top_p for models that support them
            if not "o4-mini" in self.model:
                params["temperature"] = self.temperature
                params["top_p"] = self.top_p
            
            response = await self.openai_client.chat.completions.create(**params)
            
            processing_time = self._measure_execution_time(start_time)
            
            # Extract the response text
            response_text = response.choices[0].message.content
            
            if self.verbose:
                self.logger.debug(f"Response length: {len(response_text)} characters")
                
            # Format the sources for citation
            sources = self._format_sources(retrieved_docs)
            
            # Get token usage from the response
            tokens = {
                "prompt": response.usage.prompt_tokens,
                "completion": response.usage.completion_tokens,
                "total": response.usage.total_tokens
            }
            
            if self.verbose:
                self.logger.info(f"Token usage: {tokens}")
            
            # Prepare the response data
            response_data = {
                "response": response_text,
                "sources": sources,
                "tokens": tokens["total"],
                "token_usage": tokens,
                "processing_time": processing_time
            }
            
            if self.verbose:
                self.logger.info("🔒 [OPENAI CLIENT] Calling security wrapper for non-streaming response")
            
            # Apply security checking before returning
            return await self._secure_response(response_data)
        except Exception as e:
            self.logger.error(f"Error generating response: {str(e)}")
            return {"error": f"Failed to generate response: {str(e)}"}
    
    async def generate_response_stream(
        self, 
        message: str, 
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[str, None]:
        """
        Generate a streaming response for a chat message using OpenAI.
        
        Args:
            message: The user's message
            adapter_name: Name of the adapter to use for retrieval
            system_prompt_id: Optional ID of a system prompt to use
            context_messages: Optional list of previous conversation messages
            
        Yields:
            Chunks of the response as they are generated
        """
        try:
            if self.verbose:
                self.logger.info(f"Starting streaming response for message: {message[:100]}...")
                
            # Retrieve and rerank documents
            retrieved_docs = await self._retrieve_and_rerank_docs(message, adapter_name)
            
            # Get the system prompt
            system_prompt = await self._get_system_prompt(system_prompt_id)
            
            # Format the context from retrieved documents
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
            
            # Initialize OpenAI client if not already initialized
            if not self.openai_client:
                await self.initialize()
            
            if self.verbose:
                self.logger.info(f"Calling OpenAI API with streaming enabled")
                
            # Prepare messages for the API call
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add context messages if provided
            if context_messages:
                messages.extend(context_messages)
            
            # Add the current message with context
            messages.append({
                "role": "user", 
                "content": f"Context information:\n{context}\n\nUser Query: {message}"
            })
            
            # Prepare parameters based on model
            params = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": self.max_tokens,
                "stream": True
            }
            
            # Add temperature and top_p for models that support them
            if not "o4-mini" in self.model:
                params["temperature"] = self.temperature
                params["top_p"] = self.top_p
            
            # Generate streaming response
            response_stream = await self.openai_client.chat.completions.create(**params)
            
            # Create the original stream generator
            async def original_stream():
                try:
                    chunk_count = 0
                    async for chunk in response_stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            chunk_text = chunk.choices[0].delta.content
                            chunk_count += 1
                            
                            if self.verbose and chunk_count % 10 == 0:
                                self.logger.debug(f"Received chunk {chunk_count}")
                            
                            yield json.dumps({
                                "response": chunk_text,
                                "sources": [],
                                "done": False
                            })
                    
                    if self.verbose:
                        self.logger.info(f"Streaming complete. Received {chunk_count} chunks")
                    
                    # When stream is complete, send the sources
                    sources = self._format_sources(retrieved_docs)
                    yield json.dumps({
                        "response": "",
                        "sources": sources,
                        "done": True
                    })
                except Exception as e:
                    self.logger.error(f"Error in streaming response: {str(e)}")
                    yield json.dumps({"error": f"Error in streaming response: {str(e)}", "done": True})
            
            # Apply security checking to the stream
            if self.verbose:
                self.logger.info("🔒 [OPENAI CLIENT] Calling security wrapper for streaming response")
            
            async for secure_chunk in self._secure_response_stream(original_stream()):
                yield secure_chunk
                
        except Exception as e:
            self.logger.error(f"Error generating streaming response: {str(e)}")
            yield json.dumps({"error": f"Failed to generate response: {str(e)}", "done": True}) 