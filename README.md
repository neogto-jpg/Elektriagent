# Elektriagent

Daily agent that checks tomorrow's **Estonian (EE) day-ahead electricity
prices** from [Nord Pool](https://data.nordpoolgroup.com/auction/day-ahead/price-indices?deliveryDate=latest&currency=EUR&resolutionInMinutes=60&indexNames=EE)
and pushes a summary to your phone with [ntfy](https://ntfy.sh). Days with
large price swings are sent as high-priority alerts.

It runs on GitHub Actions every day at **15:05 Tallinn time** (summer and
winter time). If prices are not published yet it retries every 10 minutes for
up to 90 minutes. If Nord Pool is unreachable it falls back to Elering's
public API, which republishes the same exchange prices.

## What you get

Every day, one notification for tomorrow (Estonian calendar day, 00–24):

```
⚡ Price alert · EE power Thu 08.10: avg 9.1 c/kWh
⚠️ Expensive (>200 €/MWh): 18:00–21:00; peak 260.0 at 19:00
🟢 Very cheap (≤0 €/MWh): 13:00–15:00; lowest -4.0 at 13:00
↕️ Big swing: 264.0 €/MWh between 13:00 (-4.0) and 19:00 (260.0)

Avg 90.8 €/MWh (9.1 c/kWh)
Min -4.0 at 13:00 · Max 260.0 at 19:00
Cheapest 3h: 12:00–15:00 (avg 2.0)

Hourly €/MWh:
00 62 | 01 55 | 02 48 | 03 41
...
```

Normal days arrive at default priority with no alert lines. Days that trigger
any rule arrive at **high priority**. Prices are the raw exchange price,
without VAT, network fees or your retailer's margin.

### Alert rules

| Rule | Triggers when | Default |
| --- | --- | --- |
| Expensive hours | any hour is above `HIGH_PRICE` | 200 €/MWh (20 c/kWh) |
| Very cheap hours | any hour is at or below `LOW_PRICE` | 0 €/MWh |
| Big swing | max − min across the day exceeds `SPREAD_PRICE` | 150 €/MWh |

## Setup (about 5 minutes)

1. **Install ntfy** on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iOS](https://apps.apple.com/app/ntfy/id1625396347)).
2. **Pick a private topic name** that is hard to guess, e.g.
   `elekter-` followed by random characters. On ntfy.sh, anyone who knows the
   topic name can read it, so treat it like a password. In the app, tap **+**
   and subscribe to that topic.
3. In this GitHub repo, go to **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret* → name `NTFY_TOPIC`, value =
     your topic name.
4. Test it: **Actions → Daily EE price check → Run workflow**. Leave *date*
   empty after ~14:00 (tomorrow's prices), or enter today's date to test any
   time. You should get a notification within a minute.

### Optional settings

Add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Meaning | Default |
| --- | --- | --- |
| `HIGH_PRICE` | expensive-hour threshold, €/MWh | `200` |
| `LOW_PRICE` | cheap-hour threshold, €/MWh | `0` |
| `SPREAD_PRICE` | daily swing threshold, €/MWh | `150` |
| `CHEAP_BLOCK_HOURS` | length of the "cheapest block" shown | `3` |
| `SHOW_HOURLY` | include the hourly price table (`true`/`false`) | `true` |
| `NTFY_SERVER` | self-hosted ntfy server URL | `https://ntfy.sh` |

If you use a protected topic or a self-hosted ntfy server with
authentication, add an access token as the secret `NTFY_TOKEN`.

## Run locally

Requires Python 3.10+. There are no dependencies.

```sh
python3 price_agent.py --dry-run                     # print tomorrow's message
python3 price_agent.py --dry-run --date 2026-10-07   # any delivery day
NTFY_TOPIC=your-topic python3 price_agent.py         # actually send
python3 -m unittest discover -s tests -t .           # run tests
```

## Notes

- GitHub disables scheduled workflows in **public** repos after 60 days with
  no commits. If that happens, re-enable the workflow in the Actions tab.
  Private repos are not affected.
- GitHub may start scheduled runs a few minutes late at busy times.
- If no prices can be fetched, you get a "prices not available" notification
  and the workflow run is marked failed.
