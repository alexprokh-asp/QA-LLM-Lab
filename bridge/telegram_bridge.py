# -*- coding: utf-8 -*-

import os
import asyncio
import aiohttp
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from dotenv import load_dotenv


# ============================================================
# ENV
# ============================================================

load_dotenv()

api_id = int(os.environ['TG_API_ID'])
api_hash = os.environ['TG_API_HASH']

BOT_USERNAME = os.environ['TG_BOT_USERNAME']

WEBHOOK_URL = os.environ.get(
    'WEBHOOK_URL',
    'http://localhost:5001/telegram_response'
)


# ============================================================
# TELEGRAM
# ============================================================

# IMPORTANT:
# Do NOT change the session name.
# This reuses the existing asp_tg.session

client = TelegramClient(
    'asp_tg',
    api_id,
    api_hash
)

# The bot currently under test. Set via TG_BOT_USERNAME in .env —
# this bridge is generic and works with any Telegram bot target,
# not tied to a specific project.
TARGET_ENTITY = None
TARGET_CHAT_ID = None


# Minimum pacing delay between outgoing sends, to avoid tripping
# Telegram's anti-spam flood protection when a probe suite fires many
# messages back to back (e.g. garak's DanInTheWild = 256 prompts).
SEND_PACING_SECONDS = 5

# If Telegram hits us with a FloodWaitError, we remember until when
# we must not send anything at all — and fast-fail with 504 in the
# meantime instead of hammering Telegram again and making the ban
# worse. Combined with garak's skip_codes: [504], this means garak
# just skips attempts during the cooldown instead of crashing.
flood_wait_until = 0.0

_last_send_time = 0.0


# ============================================================
# TELEGRAM QUEUES
# ============================================================

# Legacy queue - for Garak / backward compatibility
telegram_messages = asyncio.Queue()

# New queue - for /prompt
# Only replies from the current target bot land here
response_queue = asyncio.Queue()


# ============================================================
# TELEGRAM → WEBHOOK
#
# Legacy mechanism for Garak / n8n
# ============================================================

async def send_to_n8n(data):

    try:

        async with aiohttp.ClientSession() as session:

            async with session.post(
                WEBHOOK_URL,
                json=data
            ) as resp:

                print(
                    f"[WEBHOOK] Sent to target, "
                    f"status code: {resp.status}"
                )

    except Exception as e:

        print(
            f"[WEBHOOK ERROR] {e}"
        )


# ============================================================
# TELEGRAM EVENT HANDLER
#
# Catches incoming messages.
#
# If the message came from the current target bot:
#     → put it on response_queue
#
# All other messages:
#     → NOT used for Promptfoo
#
# The legacy telegram_messages mechanism is kept
# for Garak.
# ============================================================

@client.on(events.NewMessage(incoming=True))
async def handler(event):

    message = event.message.message
    chat_id = event.chat_id

    sender = await event.get_sender()

    sender_id = getattr(
        sender,
        'id',
        None
    )

    print(
        f"[TELEGRAM] "
        f"chat_id={chat_id} "
        f"from={sender_id}: "
        f"{message}"
    )

    # --------------------------------------------------------
    # REPLY FROM THE CURRENT TARGET BOT
    # --------------------------------------------------------

    if TARGET_CHAT_ID is not None and chat_id == TARGET_CHAT_ID:

        print(
            f"[BOT RESPONSE] {message}"
        )

        await response_queue.put(message)

    # --------------------------------------------------------
    # LEGACY MECHANISM FOR GARAK
    # --------------------------------------------------------

    data = {
        'sender': str(sender_id),
        'chat_id': chat_id,
        'message': message
    }

    asyncio.create_task(
        send_to_n8n(data)
    )


# ============================================================
# /telegram_response
#
# Legacy endpoint.
# Garak / other processes can push messages
# onto the legacy queue.
# ============================================================

async def telegram_response(request):

    try:

        data = await request.json()

        print(
            f"[TELEGRAM TARGET] {data}"
        )

        await telegram_messages.put(data)

        return web.json_response({
            "status": "ok"
        })

    except Exception as e:

        print(
            f"[TARGET ERROR] {e}"
        )

        return web.json_response(
            {
                "status": "error",
                "error": str(e)
            },
            status=500
        )


# ============================================================
# /next_message
#
# Legacy endpoint for Garak.
# ============================================================

async def next_message_http(request):

    try:

        timeout = float(
            request.query.get(
                'timeout',
                15
            )
        )

    except ValueError:

        timeout = 15

    message = await get_next_message(
        timeout=timeout
    )

    if message is None:

        return web.json_response(
            {
                'status': 'timeout',
                'message': None
            },
            status=408
        )

    return web.json_response(
        {
            'status': 'ok',
            **message
        }
    )


# ============================================================
# RECIPIENT
# ============================================================

def _resolve_recipient(recipient):

    recipient = str(
        recipient
    ).strip()

    # Telegram numeric chat ID
    if recipient.lstrip('-').isdigit():

        return int(recipient)

    # Username / @username / phone
    return recipient


# ============================================================
# /send_message
#
# Legacy endpoint.
#
# Used by Garak / existing tests to message an ARBITRARY
# recipient (not necessarily the current target bot).
#
# NOTE: intentionally NOT touched by the /prompt truncation
# patch below — this endpoint is used for other senders/
# recipients too, and must keep sending messages unmodified.
# ============================================================

async def handle_send_message(request):

    try:

        body = await request.json()

        recipient = body.get(
            'recipient'
        )

        message = body.get(
            'message'
        )

        if not recipient or not message:

            return web.json_response(
                {
                    'status': 'error',
                    'error':
                        'recipient or message missing'
                },
                status=400
            )

        target = _resolve_recipient(
            recipient
        )

        await client.send_message(
            target,
            message
        )

        print(
            f"[TELEGRAM SEND] "
            f"{recipient}: {message}"
        )

        return web.json_response({
            'status': 'ok'
        })

    except Exception as e:

        print(
            f"[SEND ERROR] {e}"
        )

        return web.json_response(
            {
                'status': 'error',
                'error': str(e)
            },
            status=500
        )


# ============================================================
# /prompt
#
# MAIN ENDPOINT FOR PROMPTFOO / GARAK
#
# Receives:
#
# {
#     "message": "..."
# }
#
# Does:
#
# 1. Clears stale replies
# 2. Sends the message to the current target bot
# 3. Waits for a reply from that specific bot
# 4. Returns the reply
# ============================================================

# Telegram hard limit: a single message can't exceed 4096 chars.
# Long-prompt probes (e.g. promptinject.HijackLongPrompt) would
# otherwise blow past this and crash client.send_message().
# Scoped to THIS endpoint only — /send_message above is left
# untouched so other senders/recipients aren't affected.
TELEGRAM_MAX_LEN = 4096


async def handle_prompt(request):

    try:

        body = await request.json()

        message = body.get(
            'message'
        )

        if not message:

            return web.json_response(
                {
                    'status': 'error',
                    'error': 'message missing'
                },
                status=400
            )

        if TARGET_ENTITY is None:

            return web.json_response(
                {
                    'status': 'error',
                    'error':
                        'Target bot entity is not initialized'
                },
                status=500
            )

        # ----------------------------------------------------
        # Telegram flood-wait cooldown gate
        #
        # If we're still inside a previously announced
        # FloodWaitError window, fail fast instead of trying to
        # send (that would only make the ban longer). Paired
        # with skip_codes: [504] in garak_config.yaml, garak
        # just skips this attempt and moves on.
        # ----------------------------------------------------

        global flood_wait_until
        global _last_send_time

        loop_now = asyncio.get_event_loop().time()

        if loop_now < flood_wait_until:

            remaining = flood_wait_until - loop_now

            print(
                f"[FLOOD WAIT] still cooling down, "
                f"{remaining:.0f}s remaining — skipping send"
            )

            return web.json_response(
                {
                    'status': 'timeout',
                    'error':
                        f'Telegram flood wait active, '
                        f'{remaining:.0f}s remaining'
                },
                status=504
            )

        # ----------------------------------------------------
        # Drop replies left over from the previous test
        # ----------------------------------------------------

        while not response_queue.empty():

            try:
                response_queue.get_nowait()

            except asyncio.QueueEmpty:
                break

        # ----------------------------------------------------
        # Truncate to Telegram's message length limit
        # ----------------------------------------------------

        if len(message) > TELEGRAM_MAX_LEN:

            print(
                f"[PROMPTFOO → BOT] truncated "
                f"{len(message)} -> {TELEGRAM_MAX_LEN} chars"
            )

            message = message[:TELEGRAM_MAX_LEN]

        else:

            print(
                f"[PROMPTFOO → BOT] {message}"
            )

        # ----------------------------------------------------
        # Pace sends so we don't trip Telegram's flood
        # protection in the first place (probe suites like
        # DanInTheWild fire hundreds of messages back to back).
        # ----------------------------------------------------

        loop_now = asyncio.get_event_loop().time()
        since_last_send = loop_now - _last_send_time

        if since_last_send < SEND_PACING_SECONDS:

            await asyncio.sleep(
                SEND_PACING_SECONDS - since_last_send
            )

        # ----------------------------------------------------
        # Send the prompt to the target bot
        # ----------------------------------------------------

        try:

            await client.send_message(
                TARGET_ENTITY,
                message
            )

            _last_send_time = asyncio.get_event_loop().time()

        except FloodWaitError as e:

            _last_send_time = asyncio.get_event_loop().time()
            flood_wait_until = _last_send_time + e.seconds + 1

            print(
                f"[FLOOD WAIT] Telegram requires {e.seconds}s "
                f"— skipping this attempt, cooling down"
            )

            return web.json_response(
                {
                    'status': 'timeout',
                    'error':
                        f'Telegram flood wait: {e.seconds}s'
                },
                status=504
            )

        # ----------------------------------------------------
        # Wait for the reply
        # ----------------------------------------------------

        try:

            response = await asyncio.wait_for(
                response_queue.get(),
                timeout=45   # keep below garak's request_timeout (60s)
            )

        except asyncio.TimeoutError:

            print(
                "[PROMPTFOO] Bot response timeout"
            )

            return web.json_response(
                {
                    'status': 'timeout',
                    'error':
                        'Target bot response timeout'
                },
                status=504
            )

        # ----------------------------------------------------
        # Return the reply to Promptfoo
        # ----------------------------------------------------

        print(
            f"[BOT → PROMPTFOO] {response}"
        )

        return web.json_response(
            {
                'output': response
            }
        )

    except Exception as e:

        print(
            f"[PROMPT ERROR] {e}"
        )

        return web.json_response(
            {
                'status': 'error',
                'error': str(e)
            },
            status=500
        )


# ============================================================
# GET NEXT MESSAGE
#
# Legacy interface for Garak.
# ============================================================

async def get_next_message(timeout=15):

    try:

        message = await asyncio.wait_for(
            telegram_messages.get(),
            timeout=timeout
        )

        return message

    except asyncio.TimeoutError:

        return None


# ============================================================
# HTTP SERVER
# ============================================================

async def start_http_server():

    app = web.Application()

    # --------------------------------------------------------
    # :5000
    # --------------------------------------------------------

    app.router.add_post(
        '/send_message',
        handle_send_message
    )

    app.router.add_post(
        '/prompt',
        handle_prompt
    )

    # --------------------------------------------------------
    # :5001
    # --------------------------------------------------------

    app.router.add_post(
        '/telegram_response',
        telegram_response
    )

    app.router.add_get(
        '/next_message',
        next_message_http
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    # --------------------------------------------------------
    # SERVER :5000
    # --------------------------------------------------------

    site_5000 = web.TCPSite(
        runner,
        'localhost',
        5000
    )

    await site_5000.start()

    print(
        "HTTP server started:"
    )

    print(
        "  POST "
        "http://localhost:5000/send_message"
    )

    print(
        "  POST "
        "http://localhost:5000/prompt"
    )

    # --------------------------------------------------------
    # SERVER :5001
    # --------------------------------------------------------

    site_5001 = web.TCPSite(
        runner,
        'localhost',
        5001
    )

    await site_5001.start()

    print(
        "Telegram Target:"
    )

    print(
        "  POST "
        "http://localhost:5001/telegram_response"
    )

    print(
        "Poll endpoint:"
    )

    print(
        "  GET "
        "http://localhost:5001/next_message"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    global TARGET_ENTITY
    global TARGET_CHAT_ID

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    await client.start()

    print(
        "Telegram client started."
    )

    print(
        "Session: asp_tg"
    )

    # --------------------------------------------------------
    # Resolve the entity of the bot under test
    # --------------------------------------------------------

    TARGET_ENTITY = await client.get_entity(
        BOT_USERNAME
    )

    TARGET_CHAT_ID = TARGET_ENTITY.id

    print(
        f"Target bot:"
    )

    print(
        f"  username = {BOT_USERNAME}"
    )

    print(
        f"  chat_id  = {TARGET_CHAT_ID}"
    )

    # --------------------------------------------------------
    # HTTP
    # --------------------------------------------------------

    await start_http_server()

    print(
        "Waiting for Telegram messages..."
    )

    # --------------------------------------------------------
    # Keep Telethon connected
    # --------------------------------------------------------

    await client.run_until_disconnected()


# ============================================================
# START
# ============================================================

if __name__ == '__main__':

    asyncio.run(main())