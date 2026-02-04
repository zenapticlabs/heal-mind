import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { RoomConfiguration } from '@livekit/protocol';

// import { APP_CONFIG_DEFAULTS } from '@'

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

type EndpointTokenRequest = {
  room_name?: string;
  participant_name?: string;
  participant_identity?: string;
  participant_metadata?: string;
  participant_attributes?: Record<string, string>;
  room_config?: unknown;
};

function getAgentName(roomConfig: unknown): string | undefined {
  if (roomConfig && typeof roomConfig === 'object') {
    const agents = (roomConfig as { agents?: unknown }).agents;
    if (Array.isArray(agents)) {
      const first = agents[0] as { agent_name?: unknown } | undefined;
      const name = first?.agent_name;
      if (typeof name === 'string') return name;
    }
  }
  return undefined;
}

// NOTE: you are expected to define the following environment variables in `.env.local`:
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;

// don't cache the results
export const revalidate = 0;

export async function POST(req: Request) {
  try {
    if (LIVEKIT_URL === undefined) {
      throw new Error('LIVEKIT_URL is not defined');
    }
    if (API_KEY === undefined) {
      throw new Error('LIVEKIT_API_KEY is not defined');
    }
    if (API_SECRET === undefined) {
      throw new Error('LIVEKIT_API_SECRET is not defined');
    }

    // Parse request body according to LiveKit endpoint token schema.
    // https://docs.livekit.io/frontends/authentication/tokens/endpoint/#endpoint-schema
    const body = (await req.json().catch(() => ({}))) as EndpointTokenRequest;
    const agentName = getAgentName(body.room_config);

    const participantName = body.participant_name ?? 'user';
    const participantIdentity =
      body.participant_identity ?? `voice_assistant_user_${Math.floor(Math.random() * 10_000)}`;
    const roomName = body.room_name ?? `voice_assistant_room_${Math.floor(Math.random() * 10_000)}`;

    const participantToken = await createParticipantToken(
      {
        identity: participantIdentity,
        name: participantName,
        metadata: body.participant_metadata,
        attributes: body.participant_attributes,
      },
      roomName,
      agentName
    );

    // Return connection details
    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantToken: participantToken,
      participantName,
    };
    const headers = new Headers({
      'Cache-Control': 'no-store',
    });
    return NextResponse.json(data, { headers });
  } catch (error) {
    if (error instanceof Error) {
      console.error(error);
      return new NextResponse(error.message, { status: 500 });
    }
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  agentName?: string
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
    // Required to allow the web client to change its own participant metadata/attributes
    // mid-session (e.g., to update prompt overrides without reconnecting).
    canUpdateOwnMetadata: true,
  };
  at.addGrant(grant);

  if (agentName) {
    at.roomConfig = new RoomConfiguration({
      agents: [{ agentName }],
    });
  }

  return at.toJwt();
}
