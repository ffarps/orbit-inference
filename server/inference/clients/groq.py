"""
Groq client implementation for LLM inference.

This module provides a Groq-specific implementation of the BaseLLMClient interface.
"""

import json
import time
import asyncio
from typing import Dict, List, Any, Optional, AsyncGenerator
import logging
import os

from ..base_llm_client import BaseLLMClient
from ..llm_client_common import LLMClientCommon

class GroqClient(BaseLLMClient, LLMClientCommon):
    """LLM client implementation for Groq."""
    
    def __init__(self, config: Dict[str, Any], retriever: Any = None,
                 reranker_service: Any = None, prompt_service: Any = None, no_results_message: str = ""):
        # Initialize base classes properly
        BaseLLMClient.__init__(self, config, retriever, reranker_service, prompt_service, no_results_message)
        LLMClientCommon.__init__(self)  # Initialize the common client
        
        # Get Groq specific configuration
        groq_config = config.get('inference', {}).get('groq', {})
        
        self.api_key = os.getenv("GROQ_API_KEY", groq_config.get('api_key', ''))
        self.model = groq_config.get('model')  # Load model directly from config
        self.temperature = groq_config.get('temperature', 0.1)
        self.top_p = groq_config.get('top_p', 0.8)
        self.max_tokens = groq_config.get('max_tokens', 1024)
        self.stream = groq_config.get('stream', True)
        self.verbose = groq_config.get('verbose', config.get('general', {}).get('verbose', False))
        
        self.groq_client = None
        self.logger = logging.getLogger(self.__class__.__name__)
        
    async def initialize(self) -> None:
        """Initialize the Groq client."""
        try:
            from groq import AsyncGroq
            
            # Initialize the dedicated Groq client
            self.groq_client = AsyncGroq(
                api_key=self.api_key
            )
            
            self.logger.info(f"Initialized Groq client with model {self.model}")
        except ImportError:
            self.logger.error("groq package not installed or outdated. Please install with: pip install -U groq==0.23.1")
            raise
        except Exception as e:
            self.logger.error(f"Error initializing Groq client: {str(e)}")
            raise
    
    async def close(self) -> None:
        """Clean up resources."""
        try:
            if self.groq_client and hasattr(self.groq_client, "close"):
                await self.groq_client.close()
                self.logger.info("Closed Groq client session")
        except Exception as e:
            self.logger.error(f"Error closing Groq client: {str(e)}")
        
        # Signal that cleanup is complete
        self.logger.info("Groq client resources released")
    
    async def verify_connection(self) -> bool:
        """
        Verify that the connection to Groq is working.
        
        Returns:
            True if connection is working, False otherwise
        """
        try:
            if not self.groq_client:
                await self.initialize()
                
            # Check if API key is provided
            if not self.api_key:
                self.logger.error("No Groq API key provided. Please set GROQ_API_KEY environment variable or configure it in the config file.")
                return False
                
            # Log masked version of the API key for debugging
            if len(self.api_key) >= 4:
                self.logger.info(f"Using Groq API key ending with: {self.api_key[-4:]}")
            
            # Simple test request to verify connection
            response = await self.groq_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello, are you there?"}
                ],
                max_tokens=10
            )
            
            # If we get here, the connection is working
            self.logger.info("Successfully connected to Groq API")
            return True
        except Exception as e:
            self.logger.error(f"Error connecting to Groq API: {str(e)}")
            return False
    
    async def generate_response(
        self, 
        message: str, 
        adapter_name: str,
        system_prompt_id: Optional[str] = None,
        context_messages: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Generate a response for a chat message using Groq.
        
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
             
            # Retrieve and rerank documents using adapter name
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
            
            # Initialize Groq client if not already initialized
            if not self.groq_client:
                await self.initialize()
            
            # Call the Groq API
            start_time = time.time()
            
            if self.verbose:
                self.logger.info(f"Calling Groq API with model: {self.model}")
            
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
            
            response = await self.groq_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                top_p=self.top_p,
                max_tokens=self.max_tokens
            )
            
            processing_time = self._measure_execution_time(start_time)
            
            # Extract the response text
            response_text = response.choices[0].message.content
            
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
            
            # Wrap response with security checking
            response_dict = {
                "response": response_text,
                "sources": sources,
                "tokens": tokens["total"],
                "token_usage": tokens,
                "processing_time": processing_time
            }
            
            return await self._secure_response(response_dict)
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
        """
        Generate a streaming response for a chat message using Groq.
        
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
            
            # Retrieve and rerank documents using adapter name
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
            
            # Initialize Groq client if not already initialized
            if not self.groq_client:
                await self.initialize()
            
            if self.verbose:
                self.logger.info(f"Calling Groq API with model: {self.model}")
            
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
            
            # Generate streaming response
            response_stream = await self.groq_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                top_p=self.top_p,
                max_tokens=self.max_tokens,
                stream=True
            )
            
            # Process the streaming response
            chunk_count = 0
            try:
                async for chunk in response_stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        chunk_text = chunk.choices[0].delta.content
                        chunk_count += 1
                        
                        if self.verbose and chunk_count % 10 == 0:
                            self.logger.debug(f"Streaming chunk {chunk_count}")
                            
                        yield json.dumps({
                            "response": chunk_text,
                            "sources": [],
                            "done": False
                        })
                
                if self.verbose:
                    self.logger.info(f"Streaming complete. Sent {chunk_count} chunks")
                
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
                
        except Exception as e:
            self.logger.error(f"Error generating streaming response: {str(e)}")
            yield json.dumps({"error": f"Failed to generate response: {str(e)}", "done": True}) 