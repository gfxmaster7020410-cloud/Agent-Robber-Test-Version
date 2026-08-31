export interface ProviderAdapter {
  chat(payload: any): Promise<any>;
}

export class OpenAIAdapter implements ProviderAdapter {
  async chat(payload: any) { /* implementation */ }
}

export class AnthropicAdapter implements ProviderAdapter {
  async chat(payload: any) { /* implementation */ }
}

export const getAdapter = (provider: string): ProviderAdapter => {
  switch (provider) {
    case 'OPENAI': return new OpenAIAdapter();
    case 'ANTHROPIC': return new AnthropicAdapter();
    default: throw new Error('Unsupported provider');
  }
};
