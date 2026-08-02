from datetime import datetime, timezone
from decimal import Decimal

from flight_assistant.models import Itinerary, PlatformOffer, Segment, TicketGroup


def seg(
    carrier: str,
    flight_no: str,
    dep: str,
    arr: str,
    dep_local: str,
    arr_local: str,
    dep_terminal: str | None = None,
    arr_terminal: str | None = None,
) -> Segment:
    return Segment(
        carrier=carrier,
        flight_no=flight_no,
        dep_airport=dep,
        dep_terminal=dep_terminal,
        arr_airport=arr,
        arr_terminal=arr_terminal,
        dep_local=datetime.fromisoformat(dep_local),
        arr_local=datetime.fromisoformat(arr_local),
    )


def itin(
    segments: list[Segment],
    *,
    tickets: list[TicketGroup] | None = None,
    duration: int = 900,
) -> Itinerary:
    if tickets is None:
        tickets = [
            TicketGroup(
                segment_idx=list(range(len(segments))),
                baggage_through_checked=True,
                source_platform="ctrip",
            )
        ]
    return Itinerary(
        segments=segments,
        tickets=tickets,
        total_duration_min=duration,
        stop_count=len(segments) - 1,
    )


def offer(platform: str, price: str) -> PlatformOffer:
    return PlatformOffer(
        platform=platform,
        price=Decimal(price),
        currency="CNY",
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        booking_url=None,
        fare_conditions_raw=None,
        confidence="confirmed",
    )
