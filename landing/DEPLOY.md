# Deploying the landing to rapidpdf.qori.land

The landing is a standalone Next.js 14 app in this `landing/` subfolder. It builds to a fully static site (verified with `next build`). The Download buttons point at the GitHub Releases `latest` asset, so they never go stale.

Deployment could not be automated end to end because no Vercel auth token was available on this machine and project creation needs a one-time sign-in. Both options below are short. Option A matches how the other Qori apps (snip, career-agent) are deployed.

## Option A — Git integration (recommended, matches the other Qori apps)

1. In the Vercel dashboard, **Add New -> Project**, import `lucasrucu/rapid-pdf`.
2. Set **Root Directory** to `landing`. Framework preset auto-detects as Next.js.
3. Deploy. The first build runs `npm install` + `next build` from `landing/`.
4. Open the project's **Settings -> Domains**, add `rapidpdf.qori.land`.

Because `qori.land` already lives in this team's Vercel DNS zone (it's on the
qori-hub project), Vercel creates the subdomain's DNS record automatically. No
manual DNS entry is needed. The domain goes live once the record propagates
(usually under a minute).

## Option B — Vercel CLI

From this `landing/` folder, with a Vercel token in `VERCEL_TOKEN`:

```bash
npx vercel link --yes --project rapid-pdf --scope lucas-devops
npx vercel deploy --prod --yes
npx vercel domains add rapidpdf.qori.land rapid-pdf --scope lucas-devops
```

## DNS

`qori.land` is Vercel-managed (nameservers point at Vercel, the zone is on the
`lucas-devops` team). Adding `rapidpdf.qori.land` to the project auto-provisions
the record. If for any reason the zone were NOT on Vercel, the record to add at
the DNS host would be:

```
Type:  CNAME
Name:  rapidpdf
Value: cname.vercel-dns.com
```
