'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { RoomEvent, TokenSource } from 'livekit-client';
import { toast } from 'sonner';
import { useSession } from '@livekit/components-react';
import { WarningIcon } from '@phosphor-icons/react/dist/ssr';
import type { AppConfig } from '@/app-config';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
import { ViewController } from '@/components/app/view-controller';
import { Button } from '@/components/ui/button';
import { Toaster } from '@/components/ui/sonner';
import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useDebugMode } from '@/hooks/useDebug';
import { getSandboxTokenSource } from '@/lib/utils';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  return null;
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [customPrompt, setCustomPrompt] = useState('');
  const [selectedLlmModel, setSelectedLlmModel] = useState<'openai/gpt-4o' | 'google/gemini-2.5-flash' | 'anthropic/claude-3-5-sonnet-20241022'>(
    'openai/gpt-4o'
  );
  const [selectedTtsModel, setSelectedTtsModel] = useState<
    'deepgram/aura-2:odysseus' | 'elevenlabs/eleven_turbo_v2_5:iP95p4xoKVk53GoZ742B' | 'elevenlabs/eleven_turbo_v2_5:IKne3meq5aSn9XLyUdCD'
  >('elevenlabs/eleven_turbo_v2_5:iP95p4xoKVk53GoZ742B');
  const openRequestIdRef = useRef<string | null>(null);

  const tokenSource = useMemo(() => {
    // If a sandbox endpoint is configured, keep using it as-is.
    // Otherwise, use the standard endpoint token flow and include participant metadata.
    if (typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string') {
      return getSandboxTokenSource(appConfig);
    }

    // TokenSource.endpoint() doesn't currently expose a typed way to pass arbitrary
    // request body fields from the browser, but the endpoint schema supports
    // `participant_metadata`.
    // See: https://docs.livekit.io/frontends/authentication/tokens/endpoint/#endpoint-schema
    return TokenSource.custom(async (options) => {
      const res = await fetch('/api/connection-details', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          room_name: options.roomName,
          participant_identity: options.participantIdentity,
          participant_name: options.participantName,
          participant_metadata: JSON.stringify({
            prompt: customPrompt,
            llm: selectedLlmModel,
            tts: selectedTtsModel,
          }),
          room_config: appConfig.agentName
            ? {
                agents: [{ agent_name: appConfig.agentName }],
              }
            : undefined,
        }),
      });

      if (!res.ok) {
        throw new Error(`Failed to fetch connection details: ${res.status} ${res.statusText}`);
      }

      const data = (await res.json()) as {
        serverUrl: string;
        roomName: string;
        participantToken: string;
        participantName: string;
      };
      return {
        serverUrl: data.serverUrl,
        roomName: data.roomName,
        participantToken: data.participantToken,
        participantName: data.participantName,
      };
    });
  }, [appConfig, customPrompt, selectedLlmModel, selectedTtsModel]);

  const session = useSession(tokenSource);

  // Load default prompt from /public/prompt.txt on frontend boot.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/prompt.txt', { cache: 'no-store' });
        if (!res.ok) return;
        const text = await res.text();
        if (cancelled) return;
        // Only set if the user hasn't typed anything yet.
        setCustomPrompt((prev) => (prev.trim().length ? prev : text));
      } catch (err) {
        console.warn('Failed to load /prompt.txt', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Listen for prompt responses from the agent and populate the textarea.
  useEffect(() => {
    const room = session?.room;
    if (!room) return;

    const decoder = new TextDecoder();
    const handler = (payload: Uint8Array, _participant: unknown, _kind: unknown, topic?: string) => {
      if (topic !== 'healmind.prompt.current') return;
      try {
        const text = decoder.decode(payload);
        const data = JSON.parse(text) as { requestId?: string | null; prompt?: string };
        const expected = openRequestIdRef.current;

        // If a requestId is set, only accept the matching response.
        if (expected && data.requestId && data.requestId !== expected) return;

        // If we requested a prompt, accept the response.
        if (typeof data.prompt === 'string') {
          setCustomPrompt(data.prompt);
        }
        // Clear request id after we handled the response.
        openRequestIdRef.current = null;
      } catch (err) {
        console.warn('Failed to parse prompt response payload', err);
      }
    };

    // livekit-client passes (payload, participant, kind, topic)
    room.on(RoomEvent.DataReceived, handler as never);
    return () => {
      room.off(RoomEvent.DataReceived, handler as never);
    };
  }, [session?.room]);

  async function requestPromptFromAgent() {
    const room = session?.room;
    const local = room?.localParticipant;
    if (!room || !local || room.state !== 'connected') {
      return;
    }

    try {
      // Use WebCrypto when available; fall back to Math.random.
      const requestId =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : String(Math.random()).slice(2);
      openRequestIdRef.current = requestId;

      const payload = new TextEncoder().encode(JSON.stringify({ requestId }));
      await local.publishData(payload, { reliable: true, topic: 'healmind.prompt.get' });
    } catch (err) {
      console.warn('Failed to request prompt from agent', err);
    }
  }

  async function applyPromptUpdate() {
    // Update local participant metadata mid-session.
    // This triggers `participant_metadata_changed` for connected clients and agents.
    const room = session?.room;
    const local = room?.localParticipant;
    if (!room || !local || room.state !== 'connected') {
      // Not connected yet (or in the middle of reconnecting). The prompt will still be
      // included on the next connection via participant_metadata in token generation.
      return;
    }

    try {
      await local.setMetadata(
        JSON.stringify({
          prompt: customPrompt,
          llm: selectedLlmModel,
          tts: selectedTtsModel,
        })
      );
      toast.success('Prompt updated');
    } catch (err) {
      // Common failure modes:
      // - SignalClient disconnected (during reconnect)
      // - Request timed out
      console.warn('Failed to update participant metadata', err);
      toast.warning('Could not update prompt right now. Try again once connected.');
    }
  }

  return (
    <AgentSessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <ViewController appConfig={appConfig} />
      </main>

      <div className="fixed left-4 top-4 z-50 flex gap-2">
        <Button
          variant="ghost"
          // Explicitly set both modes; avoid inline style (it was forcing white in dark mode).
          className="border border-black/80 bg-white text-black/80 shadow-sm transition-all hover:bg-white hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black/110
             dark:border-white/80 dark:bg-black dark:text-white/80 dark:hover:bg-black dark:focus-visible:ring-white/60 dark:hover:text-white/100 dark:hover:border-white/100" 
          onClick={async () => {
        setPromptDialogOpen(true);
        // Pull current prompt from backend (agent) to pre-fill the textarea.
        await requestPromptFromAgent();
          }}
        >
          Edit
        </Button>
      </div>

      {promptDialogOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-background w-full max-w-2xl rounded-lg border p-4 shadow-lg">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Agent prompt</h2>
              <Button variant="ghost" onClick={() => setPromptDialogOpen(false)}>
                Close
              </Button>
            </div>

            <p className="text-muted-foreground mb-2 text-sm">
              This prompt used in your agent. If
              you&apos;re already connected, saving will update it immediately.
            </p>

            <div className="mb-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <label className="flex flex-col gap-2 text-sm">
                <span className="text-muted-foreground">LLM model</span>
                <select
                  className="bg-background focus:ring-ring w-full rounded-md border px-3 py-2 text-sm outline-none focus:ring-2"
                  value={selectedLlmModel}
                  onChange={(e) =>
                    setSelectedLlmModel(
                      e.target.value as 'openai/gpt-4o' | 'google/gemini-2.5-flash' | 'anthropic/claude-3-5-sonnet-20241022'
                    )
                  }
                >
                  <option value="openai/gpt-4o">openai/gpt-4o</option>
                  <option value="anthropic/claude-3-5-sonnet-20241022">anthropic/claude-3-5-sonnet</option>
                  <option value="google/gemini-2.5-flash">google/gemini-2.5-flash</option>
                </select>
              </label>

              <label className="flex flex-col gap-2 text-sm">
                <span className="text-muted-foreground">TTS voice</span>
                <select
                  className="bg-background focus:ring-ring w-full rounded-md border px-3 py-2 text-sm outline-none focus:ring-2"
                  value={selectedTtsModel}
                  onChange={(e) =>
                    setSelectedTtsModel(
                      e.target.value as
                        | 'elevenlabs/eleven_turbo_v2_5:IKne3meq5aSn9XLyUdCD'
                        | 'elevenlabs/eleven_turbo_v2_5:iP95p4xoKVk53GoZ742B'
                    )
                  }
                >
                  {/* <option value="deepgram/aura-2:odysseus">deepgram/aura-2:odysseus</option> */}
                  <option value="elevenlabs/eleven_turbo_v2_5:iP95p4xoKVk53GoZ742B">
                    elevenlabs - Chris
                  </option>
                  <option value="elevenlabs/eleven_turbo_v2_5:IKne3meq5aSn9XLyUdCD">
                    elevenlabs - Charlie
                  </option>
                </select>
              </label>
            </div>

            <textarea
              className="bg-background focus:ring-ring min-h-40 w-full rounded-md border p-3 text-sm outline-none focus:ring-2"
              placeholder="Enter a custom system prompt / instructions for the agent..."
              value={customPrompt}
              onChange={(e) => setCustomPrompt(e.target.value)}
            />

            <div className="mt-3 flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setCustomPrompt('')}>
                Clear
              </Button>
              <Button
                onClick={async () => {
                  await applyPromptUpdate();
                  setPromptDialogOpen(false);
                }}
              >
                Save
              </Button>
            </div>
          </div>

        </div>
      ) : null}

      <StartAudioButton label="Start Audio" />
      <Toaster
        icons={{
          warning: <WarningIcon weight="bold" />,
        }}
        position="top-center"
        className="toaster group"
        style={
          {
            '--normal-bg': 'var(--popover)',
            '--normal-text': 'var(--popover-foreground)',
            '--normal-border': 'var(--border)',
          } as React.CSSProperties
        }
      />
    </AgentSessionProvider>
  );
}
