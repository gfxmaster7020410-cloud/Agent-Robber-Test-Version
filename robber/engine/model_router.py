from typing import Dict, Any

class ModelRouter:
    """Routes requests to appropriate Gemini models based on execution mode."""

    def get_route_config(self, mode: str = "Coder", context_token_count: int = 0) -> Dict[str, Any]:
        """Returns model configuration based on mode."""
        
        # Architect uses gemini-1.5-pro for 2M context
        if mode == "Architect" or (context_token_count > 1000000):
            return {
                "model": "gemini-1.5-pro",
                "temperature": 0.7,
                "top_p": 0.95,
                "system_prompt": "You are a senior systems architect. Provide comprehensive, multi-file mapping and architectural guidance."
            }
        
        # Coder mode
        if mode == "Coder":
            return {
                "model": "gemini-3.6-flash",
                "temperature": 0.0,
                "top_p": 0.1,
                "system_prompt": "You are an expert coder. Focus on AST accuracy, clean syntax, and idiomatic code."
            }
        
        # Ask (default/analysis)
        return {
            "model": "gemini-3.6-flash",
            "temperature": 0.4,
            "top_p": 0.8,
            "system_prompt": "You are a code analysis assistant. Provide clear, context-aware explanations."
        }
