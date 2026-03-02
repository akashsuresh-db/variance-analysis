import type { Session, Message } from './types'

const BASE = '/api'

export async function createSession(title = 'New Chat'): Promise<Session> {
  const r = await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  if (!r.ok) throw new Error(`Failed to create session: ${r.status}`)
  return r.json()
}

export async function listSessions(): Promise<Session[]> {
  const r = await fetch(`${BASE}/sessions`)
  if (!r.ok) throw new Error(`Failed to list sessions: ${r.status}`)
  return r.json()
}

export async function getSessionMessages(sessionId: string): Promise<{ session_id: string; title: string; messages: Message[] }> {
  const r = await fetch(`${BASE}/sessions/${sessionId}/messages`)
  if (!r.ok) throw new Error(`Failed to get messages: ${r.status}`)
  return r.json()
}

export async function updateSessionTitle(sessionId: string, title: string): Promise<void> {
  await fetch(`${BASE}/sessions/${sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export async function deleteSession(sessionId: string): Promise<void> {
  await fetch(`${BASE}/sessions/${sessionId}`, { method: 'DELETE' })
}

export interface StreamResult {
  message_id: string
  session_id: string
  role: 'assistant'
  content: string
  created_at: string
}

/**
 * Send a message and stream the response via SSE.
 * onChunk is called for each text token as it arrives.
 */
export async function sendMessage(
  sessionId: string,
  message: string,
  onChunk: (text: string) => void,
  onDone?: () => void,
): Promise<StreamResult> {
  const r = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
  if (!r.ok) {
    const err = await r.text()
    throw new Error(`Chat failed: ${err}`)
  }

  const reader = r.body!.getReader()
  const decoder = new TextDecoder()
  let sseBuffer = ''
  let messageId = ''
  let fullContent = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    sseBuffer += decoder.decode(value, { stream: true })

    // SSE messages are separated by double newlines
    const parts = sseBuffer.split('\n\n')
    sseBuffer = parts.pop() ?? ''

    for (const part of parts) {
      for (const line of part.split('\n')) {
        if (!line.startsWith('data: ')) continue
        try {
          const data = JSON.parse(line.slice(6))
          if (data.type === 'chunk') {
            fullContent += data.text
            onChunk(data.text)
          } else if (data.type === 'done') {
            messageId = data.message_id
            onDone?.()
          } else if (data.type === 'error') {
            throw new Error(data.message)
          }
        } catch (e) {
          if (e instanceof SyntaxError) continue
          throw e
        }
      }
    }
  }

  return {
    message_id: messageId || crypto.randomUUID(),
    session_id: sessionId,
    role: 'assistant',
    content: fullContent,
    created_at: new Date().toISOString(),
  }
}
