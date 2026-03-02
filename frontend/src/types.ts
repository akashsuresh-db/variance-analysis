export interface Session {
  session_id: string
  user_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface Message {
  message_id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface ChatResponse extends Message {
  role: 'assistant'
}
