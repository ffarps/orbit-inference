"""
Chat service for processing chat messages
"""

import json
import asyncio
import logging
from typing import Dict, Any, Optional, List
from bson import ObjectId  # Add this import for ObjectId
import threading
from queue import Queue

from utils.text_utils import fix_text_formatting, mask_api_key
from utils.language_detector import LanguageDetector
from config.config_manager import _is_true_value

# Configure logging
logger = logging.getLogger(__name__)

class ChatService:
    """
    Handles chat-related functionality including LLM Guard security integration.
    
    SECURITY FLOW (CRITICAL):
    ========================
    
    1. **User Message Security Check** (BEFORE any processing):
       - User messages are checked for security violations BEFORE LLM processing
       - If unsafe: Return error immediately, NO LLM inference, NO storage anywhere
       - If safe: Continue to LLM processing
    
    2. **LLM Processing** (only for safe messages):
       - Retrieve conversation context from chat history
       - Apply language detection and enhancement
       - Send to LLM for inference
       - Get response from LLM
    
    3. **Response Security Check** (BEFORE storage):
       - LLM responses are checked for security violations BEFORE storage
       - If unsafe: Return error immediately, NO storage in chat history
       - If safe: Store conversation turn in chat history
    
    CRITICAL SECURITY REQUIREMENTS:
    ===============================
    
    ✅ **Blocked Messages**: Never stored in chat history or any database
    ✅ **Blocked Responses**: Never stored in chat history or any database  
    ✅ **Audit Logging**: Security violations logged for audit (not user-facing)
    ✅ **Error Handling**: User-friendly error messages without exposing security details
    ✅ **No Interference**: Security checks don't interfere with chat history or LLM flow
    ✅ **Fail-Safe**: On security service failure, configurable fallback (allow/block)
    
    INTEGRATION POINTS:
    ===================
    
    - **Message Security Check**: Before LLM inference in both streaming and non-streaming
    - **Response Security Check**: Before chat history storage in both modes
    - **Streaming Support**: Security checks work seamlessly with streaming responses
    - **No Storage Pollution**: Unsafe content never enters MongoDB chat history
    - **Audit Trail**: Security violations logged for monitoring (admin-only)
    
    FLOW DIAGRAM:
    =============
    
    User Message → Security Check → [UNSAFE] → Block & Return Error (NO STORAGE)
                                 ↓ [SAFE]
                       LLM Processing → Response Generated
                                     ↓
                       Response Security Check → [UNSAFE] → Block & Return Error (NO STORAGE)  
                                              ↓ [SAFE]
                             Store in Chat History → Return to User
    
    This ensures that:
    - No unsafe content is ever processed by the LLM
    - No unsafe content is ever stored in chat history
    - Security violations are logged for audit purposes
    - User experience remains smooth with clear error messages
    """
    
    def __init__(self, config: Dict[str, Any], llm_client, logger_service, chat_history_service=None, llm_guard_service=None, moderator_service=None):
        self.config = config
        self.llm_client = llm_client
        self.logger_service = logger_service
        self.chat_history_service = chat_history_service
        self.llm_guard_service = llm_guard_service
        self.moderator_service = moderator_service
        self.verbose = _is_true_value(config.get('general', {}).get('verbose', False))
        
        # Chat history configuration
        self.chat_history_config = config.get('chat_history', {})
        self.chat_history_enabled = _is_true_value(self.chat_history_config.get('enabled', True))
        
        # Messages configuration
        self.messages_config = config.get('messages', {})
        
        # LLM Guard configuration
        self.llm_guard_enabled = self.llm_guard_service and self.llm_guard_service.enabled
        
        # Moderator Service configuration
        self.moderator_enabled = self.moderator_service and self.moderator_service.enabled
        
        if self.verbose:
            if self.llm_guard_enabled:
                logger.info("LLM Guard security checking enabled")
            else:
                logger.info("LLM Guard security checking disabled")
                
            if self.moderator_enabled:
                logger.info("Moderator Service security checking enabled")
            else:
                logger.info("Moderator Service security checking disabled")
        
        # Initialize language detector only if enabled
        self.language_detection_enabled = _is_true_value(config.get('general', {}).get('language_detection', True))
        if self.language_detection_enabled:
            try:
                self.language_detector = LanguageDetector(verbose=self.verbose)
                if self.verbose:
                    logger.info("Language detection enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize language detector: {str(e)}, disabling language detection")
                self.language_detection_enabled = False
                self.language_detector = None
        else:
            self.language_detector = None
            if self.verbose:
                logger.info("Language detection disabled")
                
        # Thread-safe queue for streaming responses
        self._stream_queues = {}
        self._stream_locks = {}
    
    async def _get_conversation_context(self, session_id: Optional[str]) -> List[Dict[str, str]]:
        """
        Get conversation context from history for the current session
        
        Args:
            session_id: The session identifier
            
        Returns:
            List of previous messages formatted for LLM context
        """
        if not self.chat_history_enabled or not self.chat_history_service or not session_id:
            return []
            
        try:
            # IMPORTANT: Check conversation limits BEFORE retrieving context
            # This ensures archiving happens before we get the history for this request
            await self.chat_history_service._check_conversation_limits(session_id)
            
            # Get context messages from chat history (now after any archiving)
            context_messages = await self.chat_history_service.get_context_messages(session_id)
            
            if self.verbose and context_messages:
                logger.info(f"Retrieved {len(context_messages)} context messages for session {session_id}")
                
            return context_messages
            
        except Exception as e:
            logger.error(f"Error retrieving conversation context: {str(e)}")
            return []
    
    async def _store_conversation_turn(
        self,
        session_id: Optional[str],
        user_message: str,
        assistant_response: str,
        user_id: Optional[str] = None,
        api_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Store a conversation turn in chat history
        
        Args:
            session_id: Session identifier
            user_message: The user's message
            assistant_response: The assistant's response
            user_id: Optional user identifier
            api_key: Optional API key
            metadata: Optional metadata to store
        """
        if not self.chat_history_enabled or not self.chat_history_service or not session_id:
            return
            
        try:
            await self.chat_history_service.add_conversation_turn(
                session_id=session_id,
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                api_key=api_key,
                metadata=metadata
            )
            
            if self.verbose:
                logger.info(f"Stored conversation turn for session {session_id}")
                
        except Exception as e:
            logger.error(f"Error storing conversation turn: {str(e)}")
    
    async def _log_conversation(self, query: str, response: str, client_ip: str, api_key: Optional[str] = None):
        """Log conversation asynchronously without delaying the main response."""
        try:
            await self.logger_service.log_conversation(
                query=query,
                response=response,
                ip=client_ip,
                backend=None,
                blocked=False,
                api_key=api_key
            )
        except Exception as e:
            logger.error(f"Error logging conversation: {str(e)}", exc_info=True)
    
    async def _log_request(self, message: str, client_ip: str, collection_name: str):
        """Log an incoming request"""
        if self.verbose:
            logger.info(f"Processing chat message from {client_ip}, collection: {collection_name}")
            logger.info(f"Message: {message}")
    
    async def _log_response(self, response: str, client_ip: str):
        """Log a response"""
        if self.verbose:
            logger.info(f"Generated response for {client_ip}")
            logger.info(f"Response: {response[:100]}...")  # Log just the beginning to avoid huge logs
    
    async def _detect_message_language(self, message: str) -> str:
        """
        Detect the language of a message
        
        Args:
            message: The message to analyze
            
        Returns:
            Language code (defaults to 'en' if detection fails)
        """
        # Skip language detection if disabled
        if not self.language_detection_enabled or not self.language_detector:
            return "en"
            
        try:
            detected_lang = self.language_detector.detect(message)
            if self.verbose:
                logger.info(f"Language detection result: '{detected_lang}' for message: '{message[:50]}...'")
            return detected_lang
        except Exception as lang_error:
            logger.warning(f"Language detection failed: {str(lang_error)}, defaulting to English")
            return "en"
    
    def _get_language_name(self, language_code: str) -> str:
        """
        Get human-readable language name from ISO code
        
        Args:
            language_code: Two-letter language code
            
        Returns:
            Human-readable language name
        """
        try:
            import pycountry
            try:
                language = pycountry.languages.get(alpha_2=language_code)
                return language.name if language else f"the language with code '{language_code}'"
            except (AttributeError, KeyError):
                return f"the language with code '{language_code}'"
        except ImportError:
            language_names = {
                'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
                'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'zh': 'Chinese',
                'ja': 'Japanese', 'ko': 'Korean', 'ar': 'Arabic', 'hi': 'Hindi',
                'mn': 'Mongolian'
            }
            return language_names.get(language_code, f"the language with code '{language_code}'")
    
    def _create_inference_only_language_instruction(self, language_name: str) -> str:
        """
        Create a strong language instruction for inference-only mode
        
        Args:
            language_name: Human-readable language name
            
        Returns:
            Language instruction text
        """
        return (
            f"\n\n=== LANGUAGE OVERRIDE ===\n"
            f"ATTENTION: The user has switched to {language_name}.\n"
            f"MANDATORY INSTRUCTION: You MUST respond ONLY in {language_name}.\n"
            f"IGNORE any previous conversation language patterns.\n"
            f"The user expects and requires a response in {language_name}.\n"
            f"Do NOT respond in English or any other language.\n"
            f"=== END LANGUAGE OVERRIDE ===\n"
        )
    
    def _create_full_mode_language_instruction(self, language_name: str) -> str:
        """
        Create a language instruction for full mode (system prompt enhancement)
        
        Args:
            language_name: Human-readable language name
            
        Returns:
            Language instruction text
        """
        return f"\n\nIMPORTANT: The user's message is in {language_name}. You MUST respond in {language_name} only."
    
    async def _enhance_system_prompt_with_language(self, system_prompt_id: ObjectId, language_instruction: str) -> bool:
        """
        Enhance a stored system prompt with language instruction
        
        Args:
            system_prompt_id: ID of the system prompt to enhance
            language_instruction: Language instruction to add
            
        Returns:
            True if enhancement succeeded, False otherwise
        """
        if not (hasattr(self.llm_client, 'prompt_service') and self.llm_client.prompt_service):
            return False
            
        try:
            prompt_doc = await self.llm_client.prompt_service.get_prompt_by_id(system_prompt_id)
            if prompt_doc and 'prompt' in prompt_doc:
                enhanced_prompt = prompt_doc['prompt'] + language_instruction
                self.llm_client.override_system_prompt = enhanced_prompt
                
                if self.verbose:
                    logger.info(f"Enhanced stored system prompt with language instruction")
                return True
        except Exception as prompt_error:
            logger.warning(f"Failed to retrieve/enhance system prompt: {str(prompt_error)}")
        
        return False
    
    async def _detect_and_enhance_prompt(self, message: str, system_prompt_id: Optional[ObjectId] = None) -> tuple[Optional[ObjectId], Optional[str]]:
        """
        Detect message language and prepare language enhancement
        
        Args:
            message: The chat message to detect language from
            system_prompt_id: Optional ID of the original system prompt
            
        Returns:
            Tuple of (system_prompt_id, language_instruction):
            - system_prompt_id: Original or None if enhanced in-place
            - language_instruction: Language instruction to add, or None if not needed
        """
        try:
            # Skip language detection if disabled
            if not self.language_detection_enabled:
                return system_prompt_id, None
                
            # Don't modify anything if the language detector is not available
            if not self.language_detector:
                if self.verbose:
                    logger.warning("Language detector not available, skipping language enhancement")
                return system_prompt_id, None
            
            # Detect the language of the message
            detected_lang = await self._detect_message_language(message)
            
            # Only enhance if language is not English
            if detected_lang == 'en':
                return system_prompt_id, None
            
            # Get human-readable language name
            language_name = self._get_language_name(detected_lang)
            
            # Check if we're in inference-only mode
            inference_only = self.config.get('general', {}).get('inference_only', False)
            
            if inference_only:
                # Inference-only mode: Create a very strong language instruction
                language_instruction = self._create_inference_only_language_instruction(language_name)
                
                if self.verbose:
                    logger.info(f"Inference-only mode: Creating language override for {language_name}")
                return None, language_instruction
            else:
                # Full mode: language instruction goes to system prompt
                language_instruction = self._create_full_mode_language_instruction(language_name)
                
                if self.verbose:
                    logger.info(f"Full mode: Enhancing system prompt for {language_name}")
                
                # Try to enhance stored prompt if available
                if system_prompt_id:
                    if await self._enhance_system_prompt_with_language(system_prompt_id, language_instruction):
                        return None, None
                
                # If no stored prompt available, return the language instruction
                return system_prompt_id, language_instruction
                
        except Exception as e:
            logger.error(f"Unexpected error in language detection: {str(e)}")
            return system_prompt_id, None
    
    async def _log_request_details(self, message: str, client_ip: str, collection_name: str, 
                                  system_prompt_id: Optional[ObjectId], api_key: Optional[str],
                                  session_id: Optional[str], user_id: Optional[str]):
        """
        Log detailed request information for debugging
        
        Args:
            message: The chat message
            client_ip: Client IP address  
            collection_name: Collection name to use for retrieval
            system_prompt_id: Optional system prompt ID
            api_key: Optional API key
            session_id: Optional session identifier
            user_id: Optional user identifier
        """
        await self._log_request(message, client_ip, collection_name)
        
        if not self.verbose:
            return
            
        # Mask API key for logging
        masked_api_key = "None"
        if api_key:
            masked_api_key = mask_api_key(api_key, show_last=True)
        
        logger.info(f"System prompt ID: {system_prompt_id}")
        logger.info(f"API key: {masked_api_key}")
        logger.info(f"Session ID: {session_id}")
        logger.info(f"User ID: {user_id}")
        
        # Log system prompt details if available
        if system_prompt_id and hasattr(self.llm_client, 'prompt_service') and self.llm_client.prompt_service:
            try:
                prompt_doc = await self.llm_client.prompt_service.get_prompt_by_id(system_prompt_id)
                if prompt_doc:
                    logger.info(f"Using system prompt: {prompt_doc.get('name', 'Unknown')}")
                    logger.info(f"Prompt content (first 100 chars): {prompt_doc.get('prompt', '')[:100]}...")
                else:
                    logger.warning(f"System prompt ID {system_prompt_id} not found")
            except Exception as e:
                logger.warning(f"Failed to retrieve system prompt details: {str(e)}")

    async def _prepare_llm_request_data(self, message: str, system_prompt_id: Optional[ObjectId], 
                                       session_id: Optional[str]) -> tuple[str, List[Dict[str, str]], Optional[ObjectId]]:
        """
        Prepare all data needed for the LLM request including context and language enhancements
        
        Args:
            message: The original chat message
            system_prompt_id: Optional system prompt ID
            session_id: Optional session identifier for chat history
            
        Returns:
            Tuple of (final_message, final_context_messages, enhanced_prompt_id)
        """
        # Get conversation context if session is provided
        context_messages = await self._get_conversation_context(session_id)
        
        # Detect language and get enhancement instructions
        enhanced_prompt_id, language_instruction = await self._detect_and_enhance_prompt(message, system_prompt_id)
        
        # Apply language enhancement if needed
        final_message, final_context_messages = self._apply_language_enhancement(
            message, context_messages, language_instruction
        )
        
        return final_message, final_context_messages, enhanced_prompt_id
    
    def _apply_language_enhancement(self, message: str, context_messages: List[Dict[str, str]], 
                                   language_instruction: Optional[str]) -> tuple[str, List[Dict[str, str]]]:
        """
        Apply language enhancement to message and context for inference-only mode
        
        Args:
            message: The original chat message
            context_messages: List of context messages
            language_instruction: Optional language instruction to apply
            
        Returns:
            Tuple of (final_message, final_context_messages)
        """
        final_message = message
        final_context_messages = context_messages
        
        if language_instruction and self.config.get('general', {}).get('inference_only', False):
            if self.verbose:
                # Just log that we're applying language enhancement, not the full instruction
                logger.info(f"Applying language override enhancement for inference-only mode")
            
            # In inference-only mode, we need to be strategic about language instruction placement
            # Strategy 1: Prepend the language instruction to the user message for immediate impact
            final_message = language_instruction + "\n\n" + message
            
            # Strategy 2: If we have conversation context, inject a language override message
            # This helps override the established language pattern from history
            if context_messages and len(context_messages) > 0:
                # Create a copy of context messages to avoid modifying the original
                final_context_messages = context_messages.copy()
                
                # Add a strategic language override message as the most recent context
                # This appears right before the current user message, making it highly prominent
                language_override_msg = {
                    "role": "user",
                    "content": f"Please note: I am now switching to a different language for my next question. Please respond in the same language I use in my next message, regardless of the language used in our previous conversation."
                }
                final_context_messages.append(language_override_msg)
                
                # Add assistant acknowledgment to reinforce the instruction
                ack_msg = {
                    "role": "assistant", 
                    "content": "Understood. I will respond in whatever language you use in your next message."
                }
                final_context_messages.append(ack_msg)
                
                if self.verbose:
                    logger.info(f"Added strategic language override context messages")
        
        return final_message, final_context_messages
    
    async def _generate_llm_response(self, final_message: str, collection_name: str, 
                                    enhanced_prompt_id: Optional[ObjectId], 
                                    final_context_messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Generate LLM response with proper prompt handling and cleanup
        
        Args:
            final_message: The processed message to send to LLM
            collection_name: Collection name for retrieval
            enhanced_prompt_id: Optional enhanced prompt ID
            final_context_messages: Processed context messages
            
        Returns:
            Response data from LLM
        """
        try:
            # Generate response with appropriate prompt handling
            if enhanced_prompt_id is None:
                # Using override system prompt (full mode with language enhancement) or no system prompt (inference-only)
                response_data = await self.llm_client.generate_response(
                    message=final_message,
                    collection_name=collection_name,
                    context_messages=final_context_messages
                )
                # Clear any overrides after use
                self._clear_prompt_overrides()
            else:
                # Using stored system prompt
                response_data = await self.llm_client.generate_response(
                    message=final_message,
                    collection_name=collection_name,
                    system_prompt_id=enhanced_prompt_id,
                    context_messages=final_context_messages
                )
            
            return response_data
        except Exception as e:
            logger.error(f"Error generating LLM response: {str(e)}")
            return {"error": f"Failed to generate response: {str(e)}"}
    
    def _clear_prompt_overrides(self):
        """Clear any prompt overrides after use"""
        if hasattr(self.llm_client, 'clear_override_system_prompt'):
            self.llm_client.clear_override_system_prompt()
        elif hasattr(self.llm_client, 'override_system_prompt'):
            self.llm_client.override_system_prompt = None

    async def _process_chat_base(self, message: str, client_ip: str, collection_name: str, 
                                 system_prompt_id: Optional[ObjectId] = None, api_key: Optional[str] = None,
                                 session_id: Optional[str] = None, user_id: Optional[str] = None):
        """
        Base method for processing chat messages, handling common functionality.
        
        Args:
            message: The chat message
            client_ip: Client IP address
            collection_name: Collection name to use for retrieval
            system_prompt_id: Optional system prompt ID to use
            api_key: Optional API key for authentication
            session_id: Optional session identifier for chat history
            user_id: Optional user identifier
            
        Returns:
            Tuple of (enhanced_prompt_id, response_data, metadata)
        """
        # 1. Log request details
        await self._log_request_details(message, client_ip, collection_name, system_prompt_id, 
                                       api_key, session_id, user_id)
        
        # 2. FIRST LINE OF DEFENSE: Check incoming message security BEFORE any LLM processing
        if self.llm_guard_enabled or self.moderator_enabled:
            security_result = await self._check_message_security(
                content=message,
                content_type="prompt",
                user_id=user_id,
                session_id=session_id
            )
            
            # If message is not safe, return error immediately without LLM processing or storage
            if not security_result.get("is_safe", True):
                error_response = await self._handle_security_violation(
                    security_result=security_result,
                    session_id=session_id,
                    content_type="incoming message"
                )
                
                # Log for audit purposes only (no chat history storage)
                if api_key:
                    await self._log_conversation(message, f"[BLOCKED-INCOMING] {error_response['error']}", client_ip, api_key)
                
                # Return error response - NO LLM PROCESSING, NO STORAGE
                return None, {"error": error_response["error"], "blocked": True}, {
                    "collection_name": collection_name,
                    "client_ip": client_ip,
                    "blocked": True,
                    "security_check": security_result
                }
        
        # 3. Prepare LLM request data (context, language enhancement, etc.)
        final_message, final_context_messages, enhanced_prompt_id = await self._prepare_llm_request_data(
            message, system_prompt_id, session_id
        )
        
        # 4. Generate LLM response (only for safe messages)
        response_data = await self._generate_llm_response(
            final_message, collection_name, enhanced_prompt_id, final_context_messages
        )
        
        # 5. Prepare metadata for storage
        metadata = {
            "collection_name": collection_name,
            "client_ip": client_ip
        }
            
        return enhanced_prompt_id, response_data, metadata

    async def process_chat(self, message: str, client_ip: str, collection_name: str, 
                          system_prompt_id: Optional[ObjectId] = None, api_key: Optional[str] = None,
                          session_id: Optional[str] = None, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Process a chat message and return a response
        
        Args:
            message: The chat message
            client_ip: Client IP address
            collection_name: Collection name to use for retrieval
            system_prompt_id: Optional system prompt ID to use
            api_key: Optional API key for authentication
            session_id: Optional session identifier for chat history
            user_id: Optional user identifier
        """
        try:
            # Use base processing
            _, response_data, metadata = await self._process_chat_base(
                message, client_ip, collection_name, system_prompt_id, api_key, session_id, user_id
            )
            
            # Check if this was a blocked incoming message
            if response_data.get("blocked", False):
                # This is a security block from chat service - return error immediately without any storage
                error_msg = response_data.get("error", "Message blocked by security scanner")
                
                if self.verbose:
                    logger.info(f"🛑 [CHAT SERVICE] Incoming message blocked - no LLM processing performed")
                
                # Return error in MCP protocol format - NO CHAT HISTORY STORAGE
                return {
                    "error": {
                        "code": -32603,
                        "message": error_msg
                    }
                }
            
            # Ensure response_data is a dictionary
            if not isinstance(response_data, dict):
                logger.error(f"Invalid response format: {response_data}")
                return {"error": "Invalid response format from LLM client"}
            
            # Get response text and ensure it exists
            response = response_data.get("response")
            if not response:
                logger.error("No response text in LLM response")
                return {"error": "No response generated"}
                
            # Clean and format the response
            response = fix_text_formatting(response)
            
            # Security checking is now handled at LLM client level - no need to check again here
            # The LLM client will have already blocked unsafe responses before they reach this point
            
            # Handle conversation storage with warning check and logging
            final_response, warning_added = await self._handle_conversation_storage(
                session_id, message, response, user_id, api_key, metadata, client_ip
            )
            
            # Update response_data if warning was added
            if warning_added:
                response_data["response"] = final_response
            
            return response_data
            
        except Exception as e:
            logger.error(f"Error processing chat: {str(e)}")
            return {"error": str(e)}
    
    async def process_chat_stream(self, message: str, client_ip: str, collection_name: str, 
                                 system_prompt_id: Optional[ObjectId] = None, api_key: Optional[str] = None,
                                 session_id: Optional[str] = None, user_id: Optional[str] = None):
        try:
            # FIRST LINE OF DEFENSE: Check incoming message security BEFORE any processing
            if self.llm_guard_enabled or self.moderator_enabled:
                security_result = await self._check_message_security(
                    content=message,
                    content_type="prompt",
                    user_id=user_id,
                    session_id=session_id
                )
                
                # If message is not safe, return error immediately without processing or storing
                if not security_result.get("is_safe", True):
                    error_response = await self._handle_security_violation(
                        security_result=security_result,
                        session_id=session_id,
                        content_type="incoming streaming message"
                    )
                    
                    # Log for audit purposes only (no chat history storage)
                    if api_key:
                        await self._log_conversation(message, f"[BLOCKED-INCOMING-STREAM] {error_response['error']}", client_ip, api_key)
                    
                    # Send error as a streaming response - NO STORAGE
                    error_chunk = json.dumps({
                        "error": error_response["error"],
                        "done": True,
                        "blocked": True
                    })
                    yield f"data: {error_chunk}\n\n"
                    return
            
            # Prepare LLM request data (context, language enhancement, etc.)
            final_message, final_context_messages, enhanced_prompt_id = await self._prepare_llm_request_data(
                message, system_prompt_id, session_id
            )
            
            # Prepare metadata for tracking
            metadata = {
                "client_ip": client_ip,
                "collection_name": collection_name,
                "system_prompt_id": str(system_prompt_id) if system_prompt_id else None,
                "enhanced_prompt_id": str(enhanced_prompt_id) if enhanced_prompt_id else None,
                "context_messages_count": len(final_context_messages),
                "blocked": False
            }
            
            # Generate unique stream ID for this request
            stream_id = f"stream_{session_id}_{int(asyncio.get_event_loop().time() * 1000)}"
            
            # Initialize thread-safe storage for this stream
            self._stream_queues[stream_id] = Queue()
            self._stream_locks[stream_id] = threading.Lock()
            
            # Buffer to accumulate the complete response for post-stream security check
            accumulated_text = ""
            sources = []
            stream_completed_successfully = False
            
            try:
                # Start the stream generation
                if enhanced_prompt_id:
                    # Using enhanced prompt
                    stream_generator = self.llm_client.generate_response_stream(
                        message=final_message,
                        collection_name=collection_name,
                        system_prompt_id=enhanced_prompt_id,
                        context_messages=final_context_messages
                    )
                else:
                    # Using stored system prompt
                    stream_generator = self.llm_client.generate_response_stream(
                        message=final_message,
                        collection_name=collection_name,
                        system_prompt_id=enhanced_prompt_id,
                        context_messages=final_context_messages
                    )
                
                # TRUST-THEN-VERIFY STREAMING: Stream immediately while buffering
                async for chunk in stream_generator:
                    try:
                        chunk_data = json.loads(chunk)
                        
                        # If there's an error in the chunk, handle it immediately
                        if "error" in chunk_data:
                            # Check if this is a security block from LLM client
                            if chunk_data.get("blocked", False):
                                # This is a security block from LLM client - don't store in chat history
                                if self.verbose:
                                    risk_score = chunk_data.get("risk_score", 1.0)
                                    flagged_scanners = chunk_data.get("flagged_scanners", [])
                                    logger.info(f"🛑 LLM client blocked streaming response - Risk: {risk_score:.3f}, Scanners: {flagged_scanners}")
                                
                                # Log for audit purposes only (no chat history storage)
                                if api_key:
                                    await self._log_conversation(message, f"[CLIENT-BLOCKED-STREAM] {chunk_data['error']}", client_ip, api_key)
                                
                                # Send error as a streaming response and return immediately - NO STORAGE
                                yield f"data: {chunk}\n\n"
                                return
                            
                            # Check if this is LLM-level moderation (different from security blocks)
                            # LLM moderation blocks can be stored, security blocks cannot
                            if session_id and not chunk_data.get("security_block", False) and not chunk_data.get("blocked", False):
                                await self._store_conversation_turn(
                                    session_id=session_id,
                                    user_message=message,
                                    assistant_response=f"[LLM MODERATION] {chunk_data['error']}",
                                    user_id=user_id,
                                    api_key=api_key,
                                    metadata={**metadata, "llm_moderation": True}
                                )
                        
                            # Send error as a streaming response and return immediately
                            yield f"data: {chunk}\n\n"
                            return
                        
                        # 1. STREAM IMMEDIATELY: Yield the chunk to the client for low latency
                        yield f"data: {chunk}\n\n"
                        
                        # 2. CONCURRENTLY BUFFER: Accumulate content for post-stream security check
                        if "response" in chunk_data:
                            # Clean and format the response chunk
                            cleaned_chunk = fix_text_formatting(chunk_data["response"])
                            accumulated_text += cleaned_chunk
                        
                        # Handle sources
                        if "sources" in chunk_data:
                            sources = chunk_data["sources"]
                        
                        # Handle done marker
                        if chunk_data.get("done", False):
                            stream_completed_successfully = True
                            break
                            
                    except json.JSONDecodeError:
                        logger.error(f"Error parsing chunk as JSON: {chunk}")
                        # Still yield the chunk even if we can't parse it
                        yield f"data: {chunk}\n\n"
                        continue
                
                # 3. POST-STREAM PROCESSING: Since security is handled at LLM client level, 
                # we can directly store the response if streaming completed successfully
                if accumulated_text and stream_completed_successfully:
                    # Response is already security-checked by LLM client - proceed with storage
                    final_response, warning_added = await self._handle_conversation_storage(
                        session_id, message, accumulated_text, user_id, api_key, metadata, client_ip
                    )
                    
                    # If a warning was added, send it as an additional chunk to the client
                    if warning_added:
                        warning_text = final_response[len(accumulated_text):]  # Extract just the warning part
                        warning_chunk = json.dumps({
                            "response": warning_text,
                            "done": False
                        })
                        yield f"data: {warning_chunk}\n\n"
                        
                        # Send final done marker
                        done_chunk = {"done": True}
                        if sources:
                            done_chunk["sources"] = sources
                        yield f"data: {json.dumps(done_chunk)}\n\n"
                    
                    if self.verbose:
                        logger.info(f"✅ Streaming response completed and stored safely for session {session_id}")
                
            except Exception as stream_error:
                logger.error(f"Error in stream generation: {str(stream_error)}")
                # Send error as a proper JSON chunk
                error_chunk = json.dumps({
                    "error": f"Stream generation failed: {str(stream_error)}",
                    "done": True
                })
                yield f"data: {error_chunk}\n\n"
                        
            finally:
                # Clean up stream resources
                if stream_id in self._stream_queues:
                    del self._stream_queues[stream_id]
                if stream_id in self._stream_locks:
                    del self._stream_locks[stream_id]
                
        except Exception as e:
            logger.error(f"Error processing chat stream: {str(e)}")
            error_json = json.dumps({"error": str(e), "done": True})
            yield f"data: {error_json}\n\n"

    async def _handle_conversation_storage(self, session_id: Optional[str], message: str, 
                                          accumulated_text: str, user_id: Optional[str],
                                          api_key: Optional[str], metadata: Dict[str, Any],
                                          client_ip: str) -> tuple[str, bool]:
        """
        Handle conversation storage with warning check and logging
        
        Args:
            session_id: Session identifier
            message: Original user message
            accumulated_text: Assistant response
            user_id: Optional user identifier
            api_key: Optional API key
            metadata: Metadata for storage
            client_ip: Client IP address
            
        Returns:
            Tuple of (final_response, warning_added)
        """
        # Log the complete response
        await self._log_response(accumulated_text, client_ip)
        
        # Check for conversation limit warning BEFORE storing conversation
        warning = await self._check_conversation_limit_warning(session_id)
        final_response = accumulated_text
        warning_added = False
        
        if warning:
            # Add the warning to the response for storage
            final_response = f"{accumulated_text}\n\n---\n{warning}"
            warning_added = True
            
            if self.verbose:
                logger.info(f"Added conversation limit warning for session {session_id}")
        
        # Store conversation turn in history (safe content only)
        if session_id:
            await self._store_conversation_turn(
                session_id=session_id,
                user_message=message,
                assistant_response=final_response,
                user_id=user_id,
                api_key=api_key,
                metadata=metadata
            )
        
        # Log conversation to Elasticsearch if API key is provided
        if api_key:
            await self._log_conversation(message, final_response, client_ip, api_key)
        
        return final_response, warning_added

    async def _check_conversation_limit_warning(self, session_id: Optional[str]) -> Optional[str]:
        """
        Check if the conversation is approaching the limit and return a warning if needed
        
        Args:
            session_id: The session identifier
            
        Returns:
            Warning message if approaching limit, None otherwise
        """
        if not self.chat_history_enabled or not self.chat_history_service or not session_id:
            return None
            
        try:
            # Use in-memory session message counts for accurate current count
            # This reflects the count after any archiving that may have occurred
            current_count = self.chat_history_service._session_message_counts.get(session_id, 0)
            
            # Get the maximum allowed messages for this session
            max_messages = self.chat_history_service.max_conversation_messages
            
            # Only warn when we're about to hit the limit for the FIRST time
            # After archiving, we should have room again and not keep warning
            # The warning should trigger when: current + 2 (next exchange) = max_messages
            if current_count + 2 == max_messages:
                # Get the warning message from config with fallback
                warning_template = self.messages_config.get(
                    'conversation_limit_warning',
                    "⚠️ **WARNING**: This conversation will reach {max_messages} messages after this response. "
                    "The next exchange will automatically archive older messages. "
                    "Consider starting a new conversation if you want to preserve the full context."
                )
                return warning_template.format(max_messages=max_messages)
                
            return None
            
        except Exception as e:
            logger.error(f"Error checking conversation limit: {str(e)}")
            return None

    async def _check_message_security(
        self,
        content: str,
        content_type: str = "prompt",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Check message security using LLM Guard service first, then Moderator Service
        
        Args:
            content: The content to check
            content_type: Type of content ('prompt' or 'response')
            user_id: Optional user identifier
            session_id: Optional session identifier
            
        Returns:
            Dictionary with security check results
        """
        if self.verbose:
            logger.info(f"🔍 [CHAT SERVICE SECURITY] Starting {content_type.upper()} security check (FIRST LINE OF DEFENSE)")
            logger.info(f"📝 [CHAT SERVICE SECURITY] Content preview: '{content[:100]}...' (length: {len(content)})")
            if session_id:
                logger.info(f"🆔 [CHAT SERVICE SECURITY] Session ID: {session_id}")
        
        # First check with LLM Guard service if enabled
        if not self.llm_guard_enabled:
            # Return safe result if LLM Guard is disabled
            llm_guard_result = {
                "is_safe": True,
                "risk_score": 0.0,
                "sanitized_content": content,
                "flagged_scanners": [],
                "recommendations": ["LLM Guard is disabled"]
            }
            if self.verbose:
                logger.info("⏭️ [CHAT SERVICE SECURITY] LLM Guard not enabled - skipping check")
        else:
            if self.verbose:
                logger.info(f"🛡️ [CHAT SERVICE SECURITY] Running LLM Guard check on {content_type.upper()}...")
            
            try:
                # Prepare metadata for the security check
                metadata = {}
                if session_id:
                    metadata["session_id"] = session_id
                
                # Perform LLM Guard security check
                llm_guard_result = await self.llm_guard_service.check_security(
                    content=content,
                    content_type=content_type,
                    user_id=user_id,
                    metadata=metadata
                )
                
                if self.verbose:
                    is_safe = llm_guard_result.get("is_safe", True)
                    risk_score = llm_guard_result.get("risk_score", 0.0)
                    flagged_scanners = llm_guard_result.get("flagged_scanners", [])
                    
                    if is_safe:
                        logger.info(f"✅ [CHAT SERVICE SECURITY] LLM Guard {content_type.upper()} check PASSED - Safe: {is_safe}, Risk: {risk_score:.3f}")
                        if risk_score > 0.0:
                            logger.info(f"⚠️ [CHAT SERVICE SECURITY] Low risk detected: {risk_score:.3f}")
                    else:
                        logger.warning(f"🚫 [CHAT SERVICE SECURITY] LLM Guard {content_type.upper()} check FAILED - Safe: {is_safe}, Risk: {risk_score:.3f}")
                        if flagged_scanners:
                            logger.warning(f"🚩 [CHAT SERVICE SECURITY] Flagged by scanners: {flagged_scanners}")
                
            except Exception as e:
                logger.error(f"❌ [CHAT SERVICE SECURITY] Error during LLM Guard {content_type} check: {str(e)}")
                # Return safe result on error to avoid blocking legitimate messages
                llm_guard_result = {
                    "is_safe": True,
                    "risk_score": 0.0,
                    "sanitized_content": content,
                    "flagged_scanners": [],
                    "recommendations": [f"LLM Guard check failed: {str(e)}"]
                }
        
        # If LLM Guard deems the content unsafe, block it immediately
        if not llm_guard_result.get("is_safe", True):
            if self.verbose:
                logger.warning(f"🛑 [CHAT SERVICE SECURITY] {content_type.upper()} BLOCKED by LLM Guard - stopping security chain")
            return llm_guard_result
        
        # If LLM Guard deems the content safe, then check with Moderator Service if enabled
        if self.moderator_enabled:
            if self.verbose:
                logger.info(f"🛡️ [CHAT SERVICE SECURITY] Running Moderator Service check on {content_type.upper()}...")
            
            try:
                is_safe, refusal_message = await self.moderator_service.check_safety(content)
                
                if self.verbose:
                    if is_safe:
                        logger.info(f"✅ [CHAT SERVICE SECURITY] Moderator Service {content_type.upper()} check PASSED - Safe: {is_safe}")
                    else:
                        logger.warning(f"🚫 [CHAT SERVICE SECURITY] Moderator Service {content_type.upper()} check FAILED - Safe: {is_safe}")
                        logger.warning(f"🚫 [CHAT SERVICE SECURITY] Moderator blocked content: {refusal_message}")
                
                if not is_safe:
                    if self.verbose:
                        logger.warning(f"🛑 [CHAT SERVICE SECURITY] {content_type.upper()} BLOCKED by Moderator Service")
                    # Content was flagged by Moderator Service
                    return {
                        "is_safe": False,
                        "risk_score": 1.0,
                        "sanitized_content": content,
                        "flagged_scanners": ["moderator_service"],
                        "recommendations": [refusal_message or "Content flagged by Moderator Service"]
                    }
            except Exception as e:
                logger.error(f"❌ [CHAT SERVICE SECURITY] Error during Moderator Service {content_type} check: {str(e)}")
                # If Moderator Service fails, continue with LLM Guard result
        else:
            if self.verbose:
                logger.info(f"⏭️ [CHAT SERVICE SECURITY] Moderator Service not enabled - skipping {content_type.upper()} check")
        
        # Return the LLM Guard result (which was safe, otherwise we would have returned earlier)
        if self.verbose:
            logger.info(f"✅ [CHAT SERVICE SECURITY] All {content_type.upper()} security checks PASSED - content is safe")
        
        return llm_guard_result
    
    async def _handle_security_violation(
        self,
        security_result: Dict[str, Any],
        session_id: Optional[str],
        content_type: str = "content"
    ) -> Dict[str, Any]:
        """
        Handle security violations by logging details and formatting user-friendly error messages.
        
        Args:
            security_result: Result from security check containing risk score, flagged scanners, etc.
            session_id: Optional session identifier
            content_type: Type of content being checked (for logging context)
            
        Returns:
            Dictionary with formatted error message and blocked flag
        """
        risk_score = security_result.get("risk_score", 0.0)
        flagged_scanners = security_result.get("flagged_scanners", [])
        recommendations = security_result.get("recommendations", [])
        
        # Log detailed security information for administrators
        detailed_log_msg = f"[CHAT SERVICE SECURITY] {content_type.capitalize()} blocked for session {session_id}: Risk score: {risk_score:.3f}"
        if flagged_scanners:
            detailed_log_msg += f", Flagged by: {', '.join(flagged_scanners)}"
        if recommendations:
            detailed_log_msg += f", Recommendations: {'; '.join(recommendations)}"
        logger.warning(detailed_log_msg)
        
        # Create user-friendly error message (no sensitive details)
        user_error_msg = f"{content_type.capitalize()} blocked by security scanner."
        if recommendations and len(recommendations) > 0:
            # Use the first recommendation as the reason, but sanitize it
            reason = recommendations[0]
            # Remove technical details and make it user-friendly
            reason = reason.replace("Potential ", "").replace(" detected", "").replace("Review and sanitize user input", "").strip()
            if reason:  # Only add reason if there's something left after sanitization
                user_error_msg += f" Reason: {reason}"
        
        # Return error in a consistent format
        return {"error": user_error_msg, "blocked": True}