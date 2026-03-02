import { useState, useEffect, useRef, useCallback } from 'react'
import { flushSync } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import type { Session, Message } from './types'
import {
  createSession, listSessions, getSessionMessages,
  sendMessage, deleteSession
} from './api'

// ─── Icons ──────────────────────────────────────────────────────────────────

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}
function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14H6L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4h6v2" />
    </svg>
  )
}
function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

// ─── Markdown components ─────────────────────────────────────────────────────

const mdComponents = {
  h1: ({ children }: { children?: React.ReactNode }) => (
    <h1 className="md-h1">{children}</h1>
  ),
  h2: ({ children }: { children?: React.ReactNode }) => (
    <h2 className="md-h2">{children}</h2>
  ),
  h3: ({ children }: { children?: React.ReactNode }) => (
    <h3 className="md-h3">{children}</h3>
  ),
  p: ({ children }: { children?: React.ReactNode }) => (
    <p className="md-p">{children}</p>
  ),
  strong: ({ children }: { children?: React.ReactNode }) => (
    <strong className="md-strong">{children}</strong>
  ),
  em: ({ children }: { children?: React.ReactNode }) => (
    <em className="md-em">{children}</em>
  ),
  ul: ({ children }: { children?: React.ReactNode }) => (
    <ul className="md-ul">{children}</ul>
  ),
  ol: ({ children }: { children?: React.ReactNode }) => (
    <ol className="md-ol">{children}</ol>
  ),
  li: ({ children }: { children?: React.ReactNode }) => (
    <li className="md-li">{children}</li>
  ),
  code: ({ className, children }: { className?: string; children?: React.ReactNode }) => {
    const isBlock = String(children).includes('\n') || className
    return isBlock ? (
      <pre className="md-pre"><code className="md-code-block">{children}</code></pre>
    ) : (
      <code className="md-code-inline">{children}</code>
    )
  },
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="md-table-wrap"><table className="md-table">{children}</table></div>
  ),
  thead: ({ children }: { children?: React.ReactNode }) => <thead>{children}</thead>,
  tbody: ({ children }: { children?: React.ReactNode }) => <tbody>{children}</tbody>,
  tr: ({ children }: { children?: React.ReactNode }) => <tr className="md-tr">{children}</tr>,
  th: ({ children }: { children?: React.ReactNode }) => <th className="md-th">{children}</th>,
  td: ({ children }: { children?: React.ReactNode }) => <td className="md-td">{children}</td>,
  blockquote: ({ children }: { children?: React.ReactNode }) => (
    <blockquote className="md-blockquote">{children}</blockquote>
  ),
  hr: () => <hr className="md-hr" />,
}

// ─── Types ───────────────────────────────────────────────────────────────────

interface ActiveMessage extends Message {
  loading?: boolean    // true = showing spinner (no tokens yet)
  streaming?: boolean  // true = tokens arriving (spinner gone, text growing)
  error?: string
}

// Strip any agent-trace tags that leak through server-side filtering
function cleanContent(text: string): string {
  return text
    .replace(/<\/?name[^>]*>/g, '')
    .replace(/^\s*\n/, '')
    .trim()
}

// ─── Message bubble ──────────────────────────────────────────────────────────

function MessageBubble({ msg }: { msg: ActiveMessage }) {
  const isUser = msg.role === 'user'
  const clean = cleanContent(msg.content)

  return (
    <div className={`msg-row ${isUser ? 'msg-row-user' : 'msg-row-bot'}`}>
      {!isUser && (
        <div className="bot-avatar">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="3" y="11" width="18" height="10" rx="2" />
            <circle cx="12" cy="5" r="2" />
            <path d="M12 7v4" />
            <line x1="8" y1="16" x2="8" y2="16" strokeWidth="3" />
            <line x1="12" y1="16" x2="12" y2="16" strokeWidth="3" />
            <line x1="16" y1="16" x2="16" y2="16" strokeWidth="3" />
          </svg>
        </div>
      )}
      <div className="bubble-col">
        {!isUser && <div className="bot-label">Finance Assistant</div>}
        <div className={`bubble ${isUser ? 'bubble-user' : 'bubble-bot'}`}>
          {msg.loading ? (
            <div className="dots">
              <span /><span /><span />
            </div>
          ) : msg.error ? (
            <div className="error-text">{msg.error}</div>
          ) : isUser ? (
            <span className="user-text">{clean}</span>
          ) : msg.streaming ? (
            // While streaming: plain text + blinking cursor. No markdown parsing on partial content.
            <span className="streaming-text">
              {clean}<span className="cursor" />
            </span>
          ) : (
            // Stream complete: render full markdown
            <ReactMarkdown components={mdComponents as never}>{clean}</ReactMarkdown>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Session item ────────────────────────────────────────────────────────────

function SessionItem({ session, active, onSelect, onDelete }: {
  session: Session; active: boolean; onSelect: () => void; onDelete: () => void
}) {
  return (
    <div onClick={onSelect} className={`session-item ${active ? 'session-item-active' : ''}`}>
      <div className="session-title">{session.title}</div>
      <div className="session-meta">
        {session.message_count > 0 && <span>{session.message_count} msgs</span>}
        <span>{new Date(session.updated_at).toLocaleDateString()}</span>
      </div>
      <button onClick={e => { e.stopPropagation(); onDelete() }} className="delete-btn" aria-label="Delete">
        <TrashIcon />
      </button>
    </div>
  )
}

// ─── Main App ────────────────────────────────────────────────────────────────

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ActiveMessage[]>([])
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [loadingSessions, setLoadingSessions] = useState(true)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(() => { scrollToBottom() }, [messages, scrollToBottom])

  useEffect(() => {
    listSessions().then(setSessions).catch(console.error).finally(() => setLoadingSessions(false))
  }, [])

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
  }, [input])

  const handleNewSession = async () => {
    try {
      const session = await createSession()
      setSessions(prev => [session, ...prev])
      setActiveSessionId(session.session_id)
      setMessages([])
    } catch (e) { console.error(e) }
  }

  const handleSelectSession = async (sessionId: string) => {
    if (sessionId === activeSessionId) return
    setActiveSessionId(sessionId)
    try {
      const data = await getSessionMessages(sessionId)
      setMessages(data.messages as ActiveMessage[])
    } catch (e) { console.error(e) }
  }

  const handleDeleteSession = async (sessionId: string) => {
    try {
      await deleteSession(sessionId)
      setSessions(prev => prev.filter(s => s.session_id !== sessionId))
      if (activeSessionId === sessionId) { setActiveSessionId(null); setMessages([]) }
    } catch (e) { console.error(e) }
  }

  const handleSend = async () => {
    if (!input.trim() || submitting) return
    const text = input.trim()
    if (!activeSessionId) {
      const session = await createSession()
      setSessions(prev => [session, ...prev])
      setActiveSessionId(session.session_id)
      await sendMsg(session.session_id, text)
    } else {
      await sendMsg(activeSessionId, text)
    }
  }

  const sendMsg = async (sessionId: string, text: string) => {
    const userMsg: ActiveMessage = {
      message_id: crypto.randomUUID(), session_id: sessionId,
      role: 'user', content: text, created_at: new Date().toISOString(),
    }
    const pendingId = crypto.randomUUID()
    const pendingMsg: ActiveMessage = {
      message_id: pendingId, session_id: sessionId,
      role: 'assistant', content: '', loading: true, created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, userMsg, pendingMsg])
    setInput('')
    setSubmitting(true)

    try {
      const resp = await sendMessage(sessionId, text,
        // onChunk: flushSync bypasses React 18 batching so each chunk renders immediately
        (chunk) => {
          flushSync(() => {
            setMessages(prev => prev.map(m =>
              m.message_id !== pendingId ? m
                : { ...m, loading: false, streaming: true, content: m.content + chunk }
            ))
          })
        },
        // onDone: flip streaming off so markdown renders
        () => {
          flushSync(() => {
            setMessages(prev => prev.map(m =>
              m.message_id === pendingId ? { ...m, streaming: false } : m
            ))
          })
        }
      )
      // Replace temp id with server-assigned id
      setMessages(prev => prev.map(m =>
        m.message_id === pendingId ? { ...m, message_id: resp.message_id } : m
      ))
      listSessions().then(setSessions).catch(() => {})
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : 'Unknown error'
      setMessages(prev => prev.map(m =>
        m.message_id === pendingId
          ? { ...m, loading: false, streaming: false, error: errMsg, content: errMsg }
          : m
      ))
    } finally {
      setSubmitting(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const activeSession = sessions.find(s => s.session_id === activeSessionId)

  return (
    <div className="root">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="logo">
            <span className="logo-icon">◈</span>
            <div>
              <div className="logo-title">AgentBricks</div>
              <div className="logo-sub">Finance Assistant</div>
            </div>
          </div>
          <button onClick={handleNewSession} className="new-chat-btn">
            <PlusIcon /><span>New Chat</span>
          </button>
        </div>

        <div className="section-label">Recent</div>
        <div className="session-list">
          {loadingSessions ? (
            <div className="empty-hint">Loading…</div>
          ) : sessions.length === 0 ? (
            <div className="empty-hint">No chats yet</div>
          ) : sessions.map(s => (
            <SessionItem key={s.session_id} session={s} active={s.session_id === activeSessionId}
              onSelect={() => handleSelectSession(s.session_id)}
              onDelete={() => handleDeleteSession(s.session_id)} />
          ))}
        </div>

        <div className="sidebar-footer">
          Powered by Databricks MAS + Genie
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        <header className="chat-header">
          <div className="chat-title">{activeSession?.title || 'Finance Assistant'}</div>
          <div className="chat-subtitle">Multi-Agent Supervisor · Lakebase</div>
        </header>

        <div className="messages">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="welcome-icon">◈</div>
              <h2 className="welcome-title">Finance Analytics Assistant</h2>
              <p className="welcome-desc">
                Ask questions about insurance claims, policies, premiums, and fraud indicators.
              </p>
              <div className="suggestions">
                {[
                  'What is the total claim amount across all policies?',
                  'Show claims by type with average amounts',
                  'Which claims have fraud flags set?',
                  'What is the paid vs denied claim ratio?',
                ].map(q => (
                  <button key={q} onClick={() => setInput(q)} className="suggestion-chip">{q}</button>
                ))}
              </div>
            </div>
          ) : (
            messages.map(msg => <MessageBubble key={msg.message_id} msg={msg} />)
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="input-area">
          <div className="input-wrapper">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about claims, policies, premiums…"
              className="textarea"
              rows={1}
              disabled={submitting}
            />
            <button
              onClick={handleSend}
              disabled={!input.trim() || submitting}
              className={`send-btn ${(!input.trim() || submitting) ? 'send-btn-disabled' : ''}`}
            >
              <SendIcon />
            </button>
          </div>
          <div className="input-hint">Enter to send · Shift+Enter for new line</div>
        </div>
      </main>
    </div>
  )
}
