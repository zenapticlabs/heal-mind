'use client';

import { useMemo, useState } from 'react';
import { TokenSource } from 'livekit-client';
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
  }, [appConfig, customPrompt]);

  const session = useSession(tokenSource);

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

      <div className="pointer-events-none fixed top-4 left-4 z-50 flex gap-2">
        <Button
          className="pointer-events-auto bg-black text-white/80 hover:text-white/100 shadow-sm ring-1 ring-white/80 transition-all hover:bg-black hover:ring-white/100 hover:shadow-xl hover:brightness-110 active:shadow-md focus-visible:ring-2 focus-visible:ring-white/60"
          variant="secondary"
          onClick={() => setPromptDialogOpen(true)}
        >
          Edit Prompt
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
              This prompt is stored in your LiveKit <code>participant_metadata</code>. If
              you&apos;re already connected, saving will update it immediately.
            </p>

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
