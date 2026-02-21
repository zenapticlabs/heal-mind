import logging, os, json, asyncio
import time
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
from livekit.plugins import noise_cancellation, silero, elevenlabs
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

global current_prompt, current_llm_model, current_tts_model
# Base prompt comes from prompt.txt if present.
system_prompt = _read_prompt_file()

# Current active prompt for the room/session (may be overridden by participant metadata).
current_prompt = system_prompt
current_llm_model = "openai/gpt-4o"
# IMPORTANT: LiveKit Inference expects ElevenLabs TTS as either:
#   - a descriptor string:  "elevenlabs/eleven_turbo_v2_5:<voice_id>"
#   - OR inference.TTS(model="elevenlabs/eleven_turbo_v2_5", voice="<voice_id>")
# Passing a colon-delimited model into inference.TTS(model=...) will be treated
# as the *model id* and can cause "model not found" errors.
current_tts_model = "elevenlabs/eleven_turbo_v2_5:iP95p4xoKVk53GoZ742B"


def _parse_inference_tts_descriptor(value: str | None) -> tuple[str, str] | None:
    """Parse a LiveKit Inference TTS descriptor string into (model, voice).

    Expected format: "provider/model:voice".
    Returns None if it can't be parsed.
    """

    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if ":" not in s:
        return None
    model, voice = s.split(":", 1)
    model = model.strip()
    voice = voice.strip()
    if not model or not voice:
        return None
    return model, voice


class SilenceNudger:
    """Schedules a short agent nudge after a period of user silence.

    This is intentionally implemented as a small helper instead of relying on
    `user_away_timeout`, because we want a short (7s) check-in without marking
    the user as truly "away".
    """

    def __init__(
        self,
        session: AgentSession,
        *,
        silence_seconds: float = 7.0,
        cooldown_seconds: float = 30.0,
        max_nudges: int = 3,
        nudge_instructions: str | None = None,
    ) -> None:
        self._session = session
        self._silence_seconds = float(silence_seconds)
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_nudges = int(max_nudges)
        self._nudge_instructions = (
            nudge_instructions
            or "If the user has been quiet, gently check in with a short, warm question. Keep it to one sentence."
        )

        self._task: asyncio.Task[None] | None = None
        self._last_nudge_at: float | None = None
        self._nudges_sent: int = 0
        self._closed = False
        self._scheduled = False

        @session.on("agent_state_changed")
        def _on_agent_state_changed(ev):
            # ev.new_state: "speaking" | "listening" | "away" (docs)
            if self._closed:
                return
            try:
                new_state = getattr(ev, "new_state", None)
                if new_state == "speaking":
                    self.cancel()
                    logger.debug("SilenceNudger_: Agent started speaking; cancelled nudge")
                elif new_state == "listening":
                    self.schedule()
                    logger.debug("SilenceNudger_: Agent started listening; scheduled nudge")
                elif new_state == "away":
                    # Don't auto-nudge on away by default; we handle short silence ourselves.
                    pass
            except Exception:
                logger.exception("SilenceNudger_ failed handling agent_state_changed")

        @session.on("close")
        def _on_close(_ev=None):
            self.close()

    def close(self) -> None:
        self._closed = True
        self.cancel()

    def cancel(self) -> None:
        task = self._task
        self._task = None
        if task is not None and self._scheduled and not task.done():
            task.cancel()
        self._scheduled = False

    def schedule(self) -> None:
        if self._closed:
            return
        # Already scheduled.
        if self._task is not None and self._scheduled and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        self._scheduled = True
        # logger.debug("SilenceNudger: Scheduled nudge task %s", self._task)

    def _cooldown_ok(self) -> bool:
        if self._last_nudge_at is None:
            return True
        return (time.monotonic() - self._last_nudge_at) >= self._cooldown_seconds

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._silence_seconds)

            if self._closed:
                return

            # Basic anti-spam.
            if self._nudges_sent >= self._max_nudges:
                return
            if not self._cooldown_ok():
                return

            # Don't nudge if the agent is currently speaking/thinking.
            # (AgentSession exposes agent_state_changed events; there's no hard guarantee
            # of a stable property, so we simply check active speech when available.)
            if getattr(self._session, "current_speech", None):
                return

            # Prefer LLM-generated nudges so they match persona/tone. Keep it short.
            self._last_nudge_at = time.monotonic()
            self._nudges_sent += 1
            await self._session.generate_reply(instructions=self._nudge_instructions)
            self._scheduled = False
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("SilenceNudger failed while running")

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
        return f"{instructions}\n\nAdditional instructions for this session:\n{override}"

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


@server.rtc_session(agent_name="healmind-agent")
async def my_agent(ctx: JobContext):
    global system_prompt, current_prompt, current_llm_model, current_tts_model
    nudger: SilenceNudger | None = None
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }
    # Connect first so the RTC peer connection is established before we spin up
    # heavier pipelines like avatar + TTS/STT.
    await ctx.connect()

    def _extract_prompt_override(metadata: str | None):
        if not metadata:
            return None, None, None
        try:
            meta = json.loads(metadata)
            if not isinstance(meta, dict):
                return None, None, None
            raw_prompt = meta.get("prompt")
            current_llm_model = meta.get("llm")
            current_tts_model = meta.get("tts")
            if isinstance(raw_prompt, str) and raw_prompt.strip():
                return raw_prompt.strip(), current_llm_model, current_tts_model
        except Exception:
            logger.exception("Failed parsing participant metadata")
        return None, None, None

    # Read initial participant metadata (set by the frontend in the connection token).
    # Metadata is a freeform string (typically JSON).
    prompt_override: str | None = None
    participant: rtc.Participant | None = None
    try:
        participant = await ctx.wait_for_participant()
        prompt_override, current_llm_model, current_tts_model = _extract_prompt_override(
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
        global system_prompt, current_prompt, current_llm_model, current_tts_model

        # Only apply updates from the linked participant (the human).
        # If we haven't captured it yet, accept the first participant that updates.
        if participant is not None and changed_participant.identity != participant.identity:
            return
        if participant is None:
            participant = changed_participant

        prompt_override, llm_model_override, tts_model_override = _extract_prompt_override(new_metadata)
        logger.info(
            f"Participant_metadata_changed (prompt: {len(prompt_override) if prompt_override else 0}, model: {llm_model_override}, tts: {tts_model_override})"
        )

        # Base is system_prompt from prompt.txt, overridden by participant metadata prompt.
        current_prompt = prompt_override if prompt_override else system_prompt
        current_tts_model = tts_model_override if tts_model_override else current_tts_model
        current_llm_model = llm_model_override if llm_model_override else current_llm_model

        # Persist the latest prompt to prompt.txt (source of truth across restarts).
        

        # Update live LLM instructions for the current room session.
        if prompt_override:
            _write_prompt_file(current_prompt)
            ctx.room.agent.llm.set_instructions(current_prompt)
        if llm_model_override:
            ctx.room.agent.llm.set_model(current_llm_model)
        if tts_model_override:
            ctx.room.agent.tts.set_model(current_tts_model)

    # @ctx.room.on("agent_state_changed")
    # def _on_agent_state_changed(ev):
    #     # ev.new_state: "speaking" | "listening" | "away" (docs)
    #     if nudger._closed:
    #         return
    #     try:
    #         new_state = getattr(ev, "new_state", None)
    #         if new_state == "speaking":
    #             nudger.cancel()
    #             logger.debug("SilenceNudger: Agent started speaking; cancelled nudge")
    #         elif new_state == "listening":
    #             nudger.schedule()
    #             logger.debug("SilenceNudger: Agent started listening; scheduled nudge")
    #         elif new_state == "away":
    #             # Don't auto-nudge on away by default; we handle short silence ourselves.
    #             pass
    #     except Exception:
    #         logger.exception("SilenceNudger failed handling agent_state_changed")

    # @ctx.room.on("close")
    # def _on_close(_ev=None):
    #     nudger.close()

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
        llm=inference.LLM(model=current_llm_model),
        tts=(
            (lambda mv: inference.TTS(model=mv[0], voice=mv[1]))(
                _parse_inference_tts_descriptor(current_tts_model)
                or ("elevenlabs/eleven_turbo_v2_5", "iP95p4xoKVk53GoZ742B")
            )
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        preemptive_generation=True,
    )
    
    nudger = SilenceNudger(
        session=session,
        silence_seconds=10,
        cooldown_seconds=20,
        max_nudges=3,
        nudge_instructions="The user has been silent for a while. Tell him to continue speaking.",
    )
    # nudger.start()

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

    # Nudge after 7 seconds of user silence.
    # Uses user_state_changed (speaking/listening) + an asyncio timer.
    

    # Note: to apply prompt updates to every turn, thread `prompt_override` into
    # your normal conversation loop/tooling. Here we at least include it in
    # lifecycle replies (on_enter/on_exit) and keep the latest value available
    # via `get_prompt_override`.

    # Note: no second ctx.connect() here; we already connected above.


if __name__ == "__main__":
    cli.run_app(server)
