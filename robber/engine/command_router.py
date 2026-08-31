import json
import asyncio
from typing import Any, Dict, AsyncGenerator
from robber.api import fb_shark
from robber.engine.gemini_stream import GeminiStreamEngine
from robber.engine.session_manager import SessionManager

class CommandRouter:
    def __init__(self, session_manager: SessionManager, gemini_engine: GeminiStreamEngine):
        self.session_manager = session_manager
        self.gemini_engine = gemini_engine
        
    async def process_message(self, client_id: str, raw_message: str) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Parses incoming message, routes to FB SHARK functions or Gemini Engine,
        and yields standardized JSON responses.
        """
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            yield self._format_response("error", "Malformed JSON")
            return

        # Handle both 'type' and 'action' for compatibility
        action = (data.get("action") or data.get("type") or "").lower()
        # Handle 'prompt' or 'content' or 'text'
        content = data.get("prompt") or data.get("content") or data.get("text") or ""

        # 1. Intent Detection & Routing
        if action in ["user_prompt", "prompt"]:
            async for res in self._handle_agent_interaction(client_id, content):
                yield res
        
        elif action == "command":
            yield await self._handle_direct_command(content)
            
        elif action == "ping":
            yield {"type": "pong"}

        else:
            yield self._format_response("error", f"Unknown action: {action}")

    async def _handle_direct_command(self, cmd: str) -> Dict[str, Any]:
        """Maps quick commands to FB SHARK Engine functions."""
        cmd = cmd.lower()
        try:
            if cmd == "start":
                await fb_shark.start_automation()
                return self._format_response("action_result", "Automation started", {"action_taken": "start_loop"})
            elif cmd == "stop":
                await fb_shark.stop_automation()
                return self._format_response("action_result", "Automation stopped", {"action_taken": "stop_loop"})
            elif cmd == "status":
                status = await fb_shark.get_status()
                return self._format_response("action_result", f"Status: {status['health'] if isinstance(status, dict) else status.health}", {"engine_status": status['health'] if isinstance(status, dict) else status.health})
            elif cmd == "logs":
                logs = await fb_shark.get_logs()
                return self._format_response("action_result", json.dumps(logs), {"action_taken": "fetch_logs"})
            elif cmd == "help":
                help_text = "Available commands: start, stop, status, logs, help"
                return self._format_response("action_result", help_text, {"action_taken": "help"})
            else:
                return self._format_response("error", f"Unknown command: {cmd}")
        except Exception as e:
            return self._format_response("error", str(e))

    async def _handle_agent_interaction(self, client_id: str, prompt: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Routes complex prompts to Gemini with system context."""
        try:
            # Pull state for context injection
            session = await self.session_manager.get_session(client_id)
            shark_config = fb_shark.load_config()
            shark_status = shark_config.get("status", {})
            
            system_prompt = (
                f"System Context:\n"
                f"- FB SHARK Engine Running: {shark_status.get('is_running')}\n"
                f"- Engine Health: {shark_status.get('health')}\n"
                f"- Active Instances: {shark_status.get('instances')}\n\n"
                "You are an AI assistant managing the Agent Robber framework. "
                "Help the user with their request based on this status."
            )

            # Stream result
            async for chunk in self.gemini_engine.stream_completion(
                prompt_payload=prompt,
                system_instruction=system_prompt
            ):
                yield self._format_response("stream", chunk, {"action_taken": "agent_chat"})
            
            yield self._format_response("complete", "", {"action_taken": "agent_chat"})
            
        except Exception as e:
            yield self._format_response("error", str(e))

    def _format_response(self, type: str, content: str, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        return {
            "type": type,
            "content": content,
            "metadata": metadata or {}
        }
