// integrations/opencode/index.ts — OpenCode plugin for ara-tts
// Hooks into experimental.text.complete to speak assistant responses,
// and into chat.message / event for thinking/idle state management.
//
// Install via: ara-tts install opencode
// Or copy to ~/.config/opencode/plugins/ara-tts.ts and add to opencode.json

import type { Plugin } from "@opencode-ai/plugin"

const ARA_TTS_URL = process.env.ARA_TTS_SERVER ?? "http://127.0.0.1:5111"

export default (async ({ $ }) => {
  return {
    "experimental.text.complete": async (_input: unknown, output: { text: string }) => {
      // Extract <tts> tag if present (may be wrapped in HTML comment)
      const ttsMatch = output.text.match(
        /(?:<!--\s*)?<tts(?:\s+mood="([^"]*)")?>(.*?)<\/tts>(?:\s*-->)?/s
      )
      if (ttsMatch) {
        const mood = ttsMatch[1] ?? ""
        const text = ttsMatch[2].trim()
        if (text) {
          const body: Record<string, string> = { text }
          if (mood) body.mood = mood
          await $`curl -s -X POST ${ARA_TTS_URL}/speak -H "Content-Type: application/json" -d ${JSON.stringify(JSON.stringify(body))}`
        }
      }
    },

    "chat.message": async () => {
      // User submitted a prompt — switch to thinking state
      await $`curl -s -X POST ${ARA_TTS_URL}/status -H "Content-Type: application/json" -d '{"state":"thinking"}'`
    },

    event: async ({ event }: { event: unknown }) => {
      const ev = event as { type?: string }
      if (ev?.type === "session.idle") {
        await $`curl -s -X POST ${ARA_TTS_URL}/status -H "Content-Type: application/json" -d '{"state":"idle"}'`
      }
    },
  }
}) satisfies Plugin
