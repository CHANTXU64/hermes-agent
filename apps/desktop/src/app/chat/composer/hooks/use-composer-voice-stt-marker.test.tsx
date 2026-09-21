import { act, renderHook } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'

const mocks = await vi.hoisted(async () => {
  const { atom } = await import('nanostores')
  const messages = atom<unknown[]>([])

  const useVoiceConversation = vi.fn(
    (_options: { onSubmit: (text: string) => Promise<void> | void }) => ({
      end: vi.fn(async () => undefined),
      level: 0,
      muted: false,
      start: vi.fn(async () => undefined),
      status: 'idle' as const,
      stopTurn: vi.fn(),
      toggleMute: vi.fn()
    })
  )

  return { messages, useVoiceConversation }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      assistant: { thread: { readAloudFailed: 'read aloud failed' } },
      notifications: { voice: { sayStopToEnd: (phrase: string) => phrase } },
      settings: { config: { autosaveFailed: 'autosave failed' } }
    }
  })
}))
vi.mock('../scope', () => ({
  useComposerScope: () => ({ $messages: mocks.messages }),
  useComposerSurfaceId: () => null
}))
vi.mock('./use-voice-conversation', () => ({ useVoiceConversation: mocks.useVoiceConversation }))
vi.mock('./use-voice-recorder', () => ({
  useVoiceRecorder: () => ({ dictate: vi.fn(), voiceActivityState: null, voiceStatus: 'idle' })
}))
vi.mock('./use-auto-speak-replies', () => ({ useAutoSpeakReplies: vi.fn() }))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/lib/wake-indicator', () => ({
  clearWakeIndicator: vi.fn(),
  syncWakeIndicatorWithVoice: vi.fn(() => false)
}))
vi.mock('@/store/ambient', () => ({ ownsAmbientCue: vi.fn(async () => true) }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/wake-word', () => ({
  resumeWakeAfterVoice: vi.fn(async () => undefined),
  stopClientCapture: vi.fn(async () => undefined)
}))

import { useComposerVoice } from './use-composer-voice'

beforeEach(() => {
  mocks.messages.set([])
  mocks.useVoiceConversation.mockClear()
})

/** Fork: a local-STT turn must reach the model wrapped in the same voice-origin
 *  marker the platform gateway prepends, so speech-recognition errors read as
 *  mishearings rather than deliberate wording. GPT-Live is exempt — it carries
 *  the marker out-of-band through `voiceContext`. */
test('a local-STT turn submits the voice-origin marker, not the bare transcript', async () => {
  const onSubmit = vi.fn(async () => true)

  renderHook(() =>
    useComposerVoice({
      busy: false,
      clearDraft: vi.fn(),
      disabled: false,
      focusInput: vi.fn(),
      insertText: vi.fn(),
      maxRecordingSeconds: 60,
      onSubmit,
      onTranscribeAudio: vi.fn(async () => 'check the auth log'),
      sessionId: 'voice-session',
      target: 'main'
    })
  )

  // The STT conversation receives submitVoiceTurn as its onSubmit.
  const submitVoiceTurn = mocks.useVoiceConversation.mock.calls.at(-1)?.[0]?.onSubmit

  expect(typeof submitVoiceTurn).toBe('function')
  if (typeof submitVoiceTurn !== 'function') {
    throw new Error('useVoiceConversation was not given onSubmit')
  }

  await act(async () => {
    await submitVoiceTurn('check the auth log')
  })

  expect(onSubmit).toHaveBeenCalledWith(
    '[The user sent a voice message~ Here\'s what they said: "check the auth log"]'
  )
})
