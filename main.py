from multiprocessing import Value
from multiprocessing.sharedctypes import Synchronized
import time
from dotenv import load_dotenv  # has to be the first import

load_dotenv()
from lib.delta_logging import logging, red, reset, log_formatter  # has to be the second
from queue import Empty
from typing import Any, List, Optional
from typing_extensions import Literal
from lib.interruption_detection import InterruptionDetection
from lib.porcupine import wakeup_keywords
from lib.utils import calculate_volume
from lib.chatgpt import ChatGPT, Conversation, Message, initial_message
import lib.elevenlabs as elevenlabs
from lib.whisper import Transcriber, WhisperAPITranscriber, WhisperCppTranscriber
import os
import struct
import pvporcupine
from pvrecorder import PvRecorder
import openai

picovoice_access_key = os.environ["PICOVOICE_ACCESS_KEY"]
openai.api_key = os.environ["OPENAI_API_KEY"]

logger = logging.getLogger()

porcupine = pvporcupine.create(
    access_key=picovoice_access_key,
    keyword_paths=wakeup_keywords(),
)
frame_length = porcupine.frame_length  # 512
buffer_size_when_not_listening = frame_length * 32 * 20  # keeps 20s of audio
buffer_size_on_active_listening = frame_length * 32 * 60  # keeps 60s of audio
sample_rate = 16000  # sample rate for Porcupine is fixed at 16kHz
silence_threshold = 300  # maybe need to be adjusted
silence_limit = 0.5 * 32  # 0.5 seconds of silence
speaking_minimum = 0.3 * 32  # 0.3 seconds of speaking
silence_time_to_standby = (
    10 * 32
)  # goes back to wakeup word checking after 10s of silence


RecordingState = Literal[
    "waiting_for_wakeup",
    "waiting_for_silence",
    "start_reply",
    "replying",
]


class AudioRecording:
    state: RecordingState
    conversation: Conversation = [initial_message]

    silence_frame_count: int
    speaking_frame_count: int
    recording_audio_buffer: bytearray
    recorder: PvRecorder

    chat_gpt: ChatGPT
    interruption_detection: Optional[InterruptionDetection]
    transcriber: Transcriber

    def __init__(self, recorder: PvRecorder) -> None:
        self.recorder = recorder
        self.recording_audio_buffer = bytearray()
        self.speaking_frame_count = 0
        self.chat_gpt = ChatGPT()
        self.interruption_detection = None
        self.transcriber = WhisperAPITranscriber()
        self.reset("waiting_for_silence")

    def reset(self, state):
        self.recorder.start()
        self.state = state

        self.silence_frame_count = 0

        if state == "waiting_for_silence":
            self.transcriber.restart()
            self.interruption_detection = None
        elif state == "replying":
            self.interruption_detection = InterruptionDetection()
        else:
            self.speaking_frame_count = 0
            self.recording_audio_buffer = bytearray()
            self.interruption_detection = None

    def stop(self):
        self.recorder.stop()
        self.chat_gpt.stop()
        if self.interruption_detection:
            self.interruption_detection.stop()
        self.transcriber.stop()

    def transcribe_buffer(self):
        self.transcriber.consume(self.recording_audio_buffer)
        self.recording_audio_buffer = self.recording_audio_buffer[
            -max(frame_length * 32 * 3, 0) :
        ]

    def next_frame(self):
        pcm = self.recorder.read()

        self.recording_audio_buffer.extend(struct.pack("h" * len(pcm), *pcm))
        self.drop_early_recording_audio_frames()

        if self.state == "waiting_for_wakeup":
            self.waiting_for_wakeup(pcm)

        elif self.state == "waiting_for_silence":
            self.waiting_for_silence(pcm)

        elif self.state == "start_reply":
            self.recorder.stop()
            transcription = self.transcriber.transcribe_and_stop()
            if len(transcription.strip()) == 0:
                logger.info("Transcription too small, probably a mistake, bailing out")
                self.reset("waiting_for_silence")
                return

            user_message: Message = {"role": "user", "content": transcription}
            self.conversation.append(user_message)
            self.recording_audio_buffer = self.recording_audio_buffer[-frame_length:]

            self.chat_gpt.reply(self.conversation)

            self.reset("replying")
            self.recorder.start()

        elif self.state == "replying":
            self.replying_loop(pcm)

    def is_silence(self, pcm):
        rms = calculate_volume(pcm)
        return rms < silence_threshold

    def drop_early_recording_audio_frames(self):
        if len(self.recording_audio_buffer) > (
            buffer_size_when_not_listening
            if self.state == "waiting_for_wakeup"
            else buffer_size_on_active_listening
        ):
            self.recording_audio_buffer = self.recording_audio_buffer[
                frame_length:
            ]  # drop early frames to keep just most recent audio

    def waiting_for_wakeup(self, pcm: List[Any]):
        print(f"⚪️ Waiting for wake up word...", end="\r", flush=True)
        trigger = porcupine.process(pcm)
        if trigger >= 0:
            logger.info("Detected wakeup word #%s", trigger)
            elevenlabs.play_audio_file_non_blocking("beep2.mp3")
            self.state = "start_reply"

    def waiting_for_silence(self, pcm: List[Any]):
        is_silence = self.is_silence(pcm)
        emoji = "🔈" if is_silence else "🔊"
        print(f"🔴 {red}Listening... {emoji} {reset}", end="\r", flush=True)

        if is_silence:
            self.silence_frame_count += 1
            if (
                self.speaking_frame_count < speaking_minimum
                and self.silence_frame_count >= silence_limit * 2
            ):
                self.speaking_frame_count = 0
        else:
            # Cut all empty audio from before to make it smaller
            if self.speaking_frame_count == 0:
                self.recording_audio_buffer = self.recording_audio_buffer[
                    -frame_length * 4 :
                ]
            self.speaking_frame_count += 1
            self.silence_frame_count = 0

        transcription_flush_step = 1 * 32  # 1s of audio
        if (
            self.speaking_frame_count > 0
            and (self.silence_frame_count + self.speaking_frame_count)
            % transcription_flush_step
            == 0
        ):
            self.transcribe_buffer()

        if (
            self.silence_frame_count >= silence_limit
            and self.speaking_frame_count >= speaking_minimum
        ):
            logger.info("Detected silence a while after speaking, giving a reply")
            self.transcribe_buffer()
            self.state = "start_reply"

        if self.silence_frame_count >= silence_time_to_standby:
            logger.info("Long silence time, going back to waiting for the wakeup word")
            elevenlabs.play_audio_file_non_blocking("byebye.mp3")
            self.silence_frame_count = 0
            self.speaking_frame_count = 0
            self.state = "waiting_for_wakeup"

    def replying_loop(self, pcm: List[Any]):
        if self.interruption_detection is None:
            return

        try:
            (action, data) = self.chat_gpt.get(block=False)
            if action == "assistent_message":
                self.conversation.append(data)
            elif action == "play_beep":
                self.interruption_detection.pause_for(32)
                elevenlabs.play_audio_file_non_blocking(data)
            elif action == "reply_audio_started":
                self.silence_frame_count = 0
                self.speaking_frame_count = 0
                self.interruption_detection.start_reply_interruption_check(data)
            elif action == "reply_audio_ended":
                self.interruption_detection.stop()
        except Empty:
            pass

        if self.interruption_detection.is_done():
            self.recording_audio_buffer = self.recording_audio_buffer[
                -frame_length * 2 :
            ]  # Capture the last couple frames for better follow up after assistant reply
            self.speaking_frame_count = 0
            self.reset("waiting_for_silence")
        else:
            is_silence = self.is_silence(pcm)
            interrupted = self.interruption_detection.check_for_interruption(
                pcm, is_silence
            )
            if interrupted:
                logger.info("Interrupted")
                self.chat_gpt.restart()
                # Capture the last few frames when interrupting the assistent, drop anything before that, since we don't want any echo feedbacks
                self.recording_audio_buffer = self.recording_audio_buffer[
                    -frame_length * 32 :
                ]
                self.reset("waiting_for_silence")


def main():
    start_time : Synchronized = Value("d", time.time())  # type: ignore
    log_formatter.start_time = start_time

    recorder = PvRecorder(device_index=-1, frame_length=porcupine.frame_length)
    audio_recording = AudioRecording(recorder)
    try:
        while True:
            audio_recording.next_frame()
    except KeyboardInterrupt:
        print("Stopping ...")
        audio_recording.stop()
    finally:
        recorder.delete()
        porcupine.delete()


if __name__ == "__main__":
    main()
