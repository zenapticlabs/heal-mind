import logging, os
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
from livekit.plugins import noise_cancellation, silero, tavus, elevenlabs
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("agent")

load_dotenv(".env.local")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
        )
    async def on_enter(self):
        """Called when the agent enters the session."""
        await self.session.generate_reply(
            instructions="You are a loveguru. Briefly greet the user and offer to heal their heart."
        )
    async def on_exit(self):
        await self.session.generate_reply(
            instructions="Give the user a friendly goodbye before you exit, and tell him you'll always love him.",
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
    
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }
    session = AgentSession(
        stt=inference.STT(model="elevenlabs/scribe_v2_realtime"),
        llm=inference.LLM(model="openai/gpt-4o"),
        tts=elevenlabs.TTS(
            model="eleven_multilingual_v2",   #"eleven_flash_v2_5",    #"eleven_v3",    #"eleven_turbo_v2_5",
            # voice_id="u7bRcYbD7visSINTyAT8",   #os.getenv("ELEVENLABS_VOICE_ID"),
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        preemptive_generation=True,
    )

    avatar = tavus.AvatarSession(
      replica_id=os.getenv("REPLICA_ID"),  # ID of the Tavus replica to use
      persona_id=os.getenv("PERSONA_ID")    # ID of the Tavus persona to use (see preceding section for configuration details)
   )

    await avatar.start(session, room=ctx.room)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        ),
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
