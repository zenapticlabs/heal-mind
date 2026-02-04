import logging, os, json, asyncio
from pathlib import Path
from collections.abc import Callable
from templates import SYSTEM_PROMPT
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    inference,
    room_io,
)
from livekit.plugins import noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("agent")

load_dotenv(".env.local")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompt.txt"


def _read_prompt_file() -> str:
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8")
        return text.strip("\n")
    except FileNotFoundError:
        logger.warning("prompt.txt not found at %s; falling back to templates.SYSTEM_PROMPT", PROMPT_PATH)
    except Exception:
        logger.exception("Failed reading prompt.txt from %s; falling back to templates.SYSTEM_PROMPT", PROMPT_PATH)
    return SYSTEM_PROMPT


def _write_prompt_file(text: str) -> None:
    try:
        PROMPT_PATH.write_text(text, encoding="utf-8")
    except Exception:
        logger.exception("Failed writing prompt.txt to %s", PROMPT_PATH)


# Base prompt comes from prompt.txt if present.
system_prompt = _read_prompt_file()

# Current active prompt for the room/session (may be overridden by participant metadata).
current_prompt = system_prompt

class Assistant(Agent):
    def __init__(
        self,
        *,
        instructions: str | None = None,
        get_prompt_override: Callable[[], str | None] | None = None,
    ) -> None:
        if instructions is None:
            instructions = SYSTEM_PROMPT
        super().__init__(
            instructions=instructions,
        )
        self._get_prompt_override = get_prompt_override

    def _append_prompt_override(self, instructions: str) -> str:
        if not self._get_prompt_override:
            return instructions
        override = self._get_prompt_override()
        if not override:
            return instructions

    async def on_enter(self):
        """Called when the agent enters the session."""
        await self.session.generate_reply(
            instructions=self._append_prompt_override(
                "You are a loveguru. Briefly greet the user and offer to heal their heart."
            )
        )
    async def on_exit(self):
        await self.session.generate_reply(
            instructions=self._append_prompt_override(
                "Give the user a friendly goodbye before you exit, and tell him you'll always love him."
            ),
        )
    # To add tools, use the @function_tool decorator.
    # Here's an example that adds a simple weather tool.
    # You also have to add `from livekit.agents import function_tool, RunContext` to the top of this file
    # @function_tool
    # async def lookup_weather(self, context: RunContext, location: str):
    #     """Use this tool to look up current weather information in the given location.
    #
    #     If the location is not supported by the weather service, the tool will indicate this. You must tell the user the location's weather is unavailable.
    #
    #     Args:
    #         location: The location to look up weather information for (e.g. city name)
    #     """
    #
    #     logger.info(f"Looking up weather for {location}")
    #
    #     return "sunny with a temperature of 70 degrees."


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def my_agent(ctx: JobContext):
    global system_prompt, current_prompt
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }
    # Connect first so the RTC peer connection is established before we spin up
    # heavier pipelines like avatar + TTS/STT.
    await ctx.connect()

    def _extract_prompt_override(metadata: str | None) -> str | None:
        if not metadata:
            return None
        try:
            meta = json.loads(metadata)
            if not isinstance(meta, dict):
                return None
            raw_prompt = meta.get("prompt")
            if isinstance(raw_prompt, str) and raw_prompt.strip():
                return raw_prompt.strip()
        except Exception:
            logger.exception("Failed parsing participant metadata")
        return None

    # Read initial participant metadata (set by the frontend in the connection token).
    # Metadata is a freeform string (typically JSON).
    prompt_override: str | None = None
    participant: rtc.Participant | None = None
    try:
        participant = await ctx.wait_for_participant()
        prompt_override = _extract_prompt_override(
            getattr(participant, "metadata", None) if participant else None
        )
    except Exception:
        logger.exception("Failed reading participant metadata for prompt override")

    # Callback-style prompt updates: when the frontend calls setMetadata(..),
    # LiveKit emits participant_metadata_changed.
    @ctx.room.on("participant_metadata_changed")
    def on_participant_metadata_changed(
        changed_participant: rtc.Participant, old_metadata: str, new_metadata: str
    ):
        nonlocal prompt_override, participant
        global system_prompt, current_prompt

        # Only apply updates from the linked participant (the human).
        # If we haven't captured it yet, accept the first participant that updates.
        if participant is not None and changed_participant.identity != participant.identity:
            return
        if participant is None:
            participant = changed_participant

        prompt_override = _extract_prompt_override(new_metadata)
        logger.info(
            "Prompt override updated via participant_metadata_changed (len=%s)",
            len(prompt_override) if prompt_override else 0,
        )

        # Base is system_prompt from prompt.txt, overridden by participant metadata prompt.
        current_prompt = prompt_override if prompt_override else system_prompt

        # Persist the latest prompt to prompt.txt (source of truth across restarts).
        _write_prompt_file(current_prompt)

        # Update live LLM instructions for the current room session.
        ctx.room.agent.llm.set_instructions(current_prompt)

    # Data-packet protocol for prompt edit UX.
    # Frontend publishes: topic="healmind.prompt.get", payload={"requestId": "..."}
    # Agent responds:      topic="healmind.prompt.current", payload={"requestId": "...", "prompt": "..."}
    #
    # (We only send to the requesting participant identity to avoid leaking prompt text.)
    @ctx.room.on("data_received")
    def on_data_received(packet: rtc.DataPacket):
        nonlocal participant
        global current_prompt
        try:
            topic = getattr(packet, "topic", None)
            if topic != "healmind.prompt.get":
                return

            # If we already know the linked participant, only accept requests from them.
            if participant is not None and packet.participant.identity != participant.identity:
                return
            if participant is None:
                participant = packet.participant

            raw = packet.data
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")

            request_id: str | None = None
            try:
                body = json.loads(raw) if raw else {}
                if isinstance(body, dict):
                    rid = body.get("requestId")
                    if isinstance(rid, str):
                        request_id = rid
            except Exception:
                logger.warning("Failed to parse prompt.get data request payload", exc_info=True)
                pass

            payload = json.dumps({"requestId": request_id, "prompt": current_prompt})

            task = asyncio.create_task(
                ctx.room.local_participant.publish_data(
                    payload,
                    reliable=True,
                    destination_identities=[packet.participant.identity],
                    topic="healmind.prompt.current",
                )
            )
            logger.info("Responded to prompt.get data request: Launched task %s", task)
        except Exception:
            logger.exception("Failed handling data_received prompt request")

    session = AgentSession(
        stt=inference.STT(model="elevenlabs/scribe_v2_realtime"),
        llm=inference.LLM(model="openai/gpt-4o"),
        tts=__import__("livekit.plugins.elevenlabs", fromlist=["TTS"]).TTS(
            model="eleven_multilingual_v2",
            # voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        preemptive_generation=True,
    )

    tavus = __import__("livekit.plugins.tavus", fromlist=["AvatarSession"])
    avatar = tavus.AvatarSession(
        replica_id=os.getenv("REPLICA_ID"),
        persona_id=os.getenv("PERSONA_ID"),
    )

    await avatar.start(session, room=ctx.room)

    # Start the session, which initializes the voice pipeline and warms up the models
    # Use prompt.txt system_prompt as the base instructions.
    # prompt_override (from participant metadata) is appended for turn-level instructions.
    agent = Assistant(instructions=current_prompt, get_prompt_override=lambda: prompt_override)
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        ),
    )

    # Note: to apply prompt updates to every turn, thread `prompt_override` into
    # your normal conversation loop/tooling. Here we at least include it in
    # lifecycle replies (on_enter/on_exit) and keep the latest value available
    # via `get_prompt_override`.

    # Note: no second ctx.connect() here; we already connected above.


if __name__ == "__main__":
    cli.run_app(server)
