"""
v4.58: расшифровка голоса в ОТДЕЛЬНОМ процессе (python -m core.transcribe_worker <audio>).
stdin — JSON {"model", "prompt"}; stdout — JSON {"text", "duration"}.

Зачем: Whisper в процессе бота держал ~0.4 ГБ постоянно + до 0.6 ГБ на расшифровку и не отдавал их обратно.
С эмбеддингами (~0.6 ГБ) и библиотеками бот упирался в MemoryHigh → ядро тормозило процесс → голосовые
расшифровывались ЧАСАМИ (до 12 ч), копились параллельно → OOM-kill (6 раз за неделю 10–16.09.2026).
Отдельный процесс отдаёт всю память по завершении, и его можно убить по таймауту.
"""
import json
import sys


def main() -> int:
    path = sys.argv[1]
    try:
        cfg = json.loads(sys.stdin.read() or "{}")
    except Exception:
        cfg = {}
    from faster_whisper import WhisperModel
    model = WhisperModel(cfg.get("model") or "small", device="cpu", compute_type="int8", cpu_threads=2)
    segments, info = model.transcribe(path, language="ru", beam_size=5, vad_filter=True,
                                      initial_prompt=cfg.get("prompt") or None,
                                      condition_on_previous_text=False)
    text = " ".join(seg.text.strip() for seg in segments)
    sys.stdout.write(json.dumps({"text": text, "duration": round(float(info.duration or 0), 1)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
