# Movie Watches

Get a Telegram (or any [Apprise](https://github.com/caronc/apprise)) alert the moment
BookMyShow or District opens the shows you actually want: a Dolby Cinema, PCX or
HDR screen, one language, a late show, or a new IMAX version.

It has two parts. Both run alongside [changedetection.io](https://github.com/dgtlmoon/changedetection.io),
which does the scheduling and sends the notifications.

- **`app.py`: the helper (`bms-seats`).** A small HTTP service that turns BookMyShow and
  District pages into short, stable text. changedetection polls it. The text only
  changes when something you care about appears, so every change is worth an alert.
- **`dashboard.py` + `dashboard.html`: the dashboard (`watch-dashboard`).** A web page to add,
  edit, pause, mute and delete watches without hand-writing helper URLs.

## Helper routes

| Route | What it lists |
|---|---|
| `/screens/<venue>/<YYYYMMDD>[,<YYYYMMDD>…]?movie=<slug part>&screen=<regex>` | Shows of a movie at one theatre whose screen or format matches, e.g. `DOLBY CINEMA` or `PCX\|HDR`, with seat-layout links |
| `/lang/<ET…>[,<ET…>]/<language>?dates=<YYYYMMDD,…>` | Per date: theatres and formats where that language is bookable |
| `/formats/<ET…>` | The language and format versions BookMyShow offers, each with its own event code |
| `/late/<ET…>/<YYYYMMDD>?from=HHMM` | Citywide shows starting at or after a time |
| `/district/<language\|any>/<district page slug>[?date=YYYY-MM-DD]` | Whether District bookings are open |
| `/district-late/<district page slug>?date=YYYY-MM-DD&from=HHMM` | District shows starting at or after a time |
| `/releases/<ET…>/<YYYYMMDD>/<venue>` | Seats released back into a sold-out show |
| `/last?path=<route path and query>` | The last successful reply for that route, as JSON `{age, text}` (used by the dashboard) |

Query `region` (default `HYD`) and `city` (default derived from region) pick the city.

## Dashboard

- Watches grouped by movie with posters, each with a status light: checking, paused or muted, or needs a look.
- Each watch lists the shows it currently sees, like "Nov 20, 8:15 AM, PCX Screen", with a **Book** link to the seat
  layout. These come from the helper's last reply to changedetection (`/last?path=…`), so they cost no extra BookMyShow requests.
- **Booked it** on a movie deletes all of that movie's watches in one go.
- **Health alerts:** every 5 minutes the dashboard checks that each watch is being checked on schedule and isn't
  failing. If a problem shows up on two checks in a row, it sends one message through changedetection's notification
  URLs, and another when the watch recovers.
- Add a watch by picking a movie from the city's BookMyShow listings. Choose premium screens, a language, new formats, District, late shows or any other link.
- Preview what a watch will see before adding it.
- Editing a watch's dates or link mutes it until its next check, so the edit doesn't send an alert.
- Every watch gets its own check interval. Watches created through changedetection's API otherwise fall back to the global default interval.

The venue presets in `dashboard.html` (`VENUES`) are Allu Cinemas Kokapet (Dolby) and Prasads
(PCX/HDR) in Hyderabad. Change them to your theatres' BookMyShow codes.

The dashboard has no login. Run it only on a network you trust.

## Running

```sh
cp .env.example .env                       # BMS_SEAT_KEY
cp dashboard.env.example dashboard.env     # CD_API_KEY from changedetection Settings > API
docker compose -f compose.example.yml up -d --build
```

The image builds on the changedetection.io image because BookMyShow's show-list API accepts
its HTTP client.

## Notes

- BookMyShow quietly serves the nearest date with shows when the requested date has none.
  The helper checks the date on every page before counting it.
- Changing the helper's output text makes changedetection alert once. Mute the affected
  watches, let them recheck, then unmute.
- This scrapes public pages for personal use. Keep the check intervals polite.

## License

[MIT](LICENSE)
