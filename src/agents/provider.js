// Unified AI provider: supports Claude (Anthropic) and Ollama (local)

async function callClaude(apiKey, systemPrompt, userContent) {
  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-6',
      max_tokens: 4096,
      system: systemPrompt,
      messages: [{ role: 'user', content: userContent }],
    }),
  });

  if (!response.ok) {
    const err = await response.text();
    throw new Error(`Claude API error ${response.status}: ${err}`);
  }

  const data = await response.json();
  return data.content[0].text;
}

async function callOllama(baseUrl, model, systemPrompt, userContent) {
  const response = await fetch(`${baseUrl}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model,
      stream: false,
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user',   content: userContent },
      ],
      options: { temperature: 0.1, num_ctx: 8192 },
    }),
  });

  if (!response.ok) {
    const err = await response.text();
    throw new Error(`Ollama error ${response.status}: ${err}`);
  }

  const data = await response.json();
  return data.message?.content || '';
}

export async function callAI(config, systemPrompt, userContent) {
  if (config.provider === 'ollama') {
    return callOllama(
      config.ollamaUrl || 'http://localhost:11434',
      config.ollamaModel || 'codellama',
      systemPrompt,
      userContent
    );
  }
  // Default: Claude
  return callClaude(config.anthropicKey, systemPrompt, userContent);
}

export function parseJSON(text) {
  // Strip markdown code fences if present
  const clean = text.replace(/^```(?:json)?\n?/m, '').replace(/\n?```$/m, '').trim();
  return JSON.parse(clean);
}
