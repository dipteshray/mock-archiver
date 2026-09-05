"""Verify a delivered message really has NO forward header. Read-only.

    python verify_no_author.py

`drop_author=True` is Telegram's "hide sender name". Proof is on the destination
message itself: `fwd_from` must be None, and the caption must equal the source's
byte for byte. Reading the D1 row is not proof - only the live message is.
"""
import asyncio
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from telethon import TelegramClient              # noqa: E402

import config                                     # noqa: E402
import mockdb                                     # noqa: E402
import sources                                    # noqa: E402


async def main():
    rows = mockdb.d1(
        'SELECT group_id, topic_id, msg_id, dest_msg_id, file_name, caption, '
        "delivery FROM mock_files WHERE status = 'done' AND dest_msg_id > 0 "
        'ORDER BY ts DESC LIMIT 5')
    if not rows:
        sys.exit('no delivered rows yet')

    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.connect()
    dest = await client.get_entity(sources.DEST)

    ok = True
    for r in rows:
        dmsg = await client.get_messages(dest, ids=r['dest_msg_id'])
        if dmsg is None:
            print(f"  MISSING dest msg {r['dest_msg_id']}")
            ok = False
            continue
        src_ent = await client.get_entity(r['group_id'])
        smsg = await client.get_messages(src_ent, ids=r['msg_id'])

        fwd = getattr(dmsg, 'fwd_from', None)
        same = (dmsg.message or '') == ((smsg.message or '') if smsg else None)
        print(f"\n  {(r['file_name'] or '')[:56]}  [{r['delivery']}]")
        print(f"    fwd_from        : {fwd!r}"
              f"   {'OK - no header' if fwd is None else 'HEADER PRESENT'}")
        print(f"    caption identical: {same}")
        if not same and smsg:
            print(f"      source: {(smsg.message or '')[:70]!r}")
            print(f"      dest  : {(dmsg.message or '')[:70]!r}")
        if fwd is not None or not same:
            ok = False

    print(f"\n{'ALL CHECKS PASSED' if ok else 'PROBLEMS FOUND'}")
    await client.disconnect()


asyncio.run(main())
