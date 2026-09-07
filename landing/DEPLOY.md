# Deploying the landing

The landing is a standalone Next.js 14 app in this `landing/` subfolder. The
Download buttons point at the GitHub Releases `latest` assets, so they never go
stale.

## Live

Deployed to Vercel (team `lucas-devops`, project `rapid-pdf`, root directory
`landing`) and serving at **https://rapidpdf.qori.land**. Because `qori.land`
lives in this team's Vercel DNS zone, the subdomain record was provisioned
automatically when the domain was attached.

## Redeploy

**A push to `main` is the redeploy.** The Vercel Git integration is connected to
`lucasrucu/rapid-pdf` with root directory `landing`, so production rebuilds on
its own. Verified 2026-09-07: pushing `b9beb17` produced production deployment
`dpl_H4s4FxEc28oUhpZ3ZppZCgchAdxk`, READY, with no manual step.

This section used to say the integration was not connected and that you had to
deploy by hand, which stopped being true at some point and nobody updated it.

The manual path still works if you need to ship without a commit. From this
`landing/` folder, signed in to the `lucas-devops` scope:

```bash
vercel deploy --prod --yes --scope lucas-devops
```

The project is linked (`.vercel/` is gitignored). The first sign-in uses
`vercel login` (device-code flow).

## DNS

`qori.land` is Vercel-managed (nameservers point at Vercel). Adding a subdomain
to the project auto-provisions the record. If the zone were ever NOT on Vercel,
the record to add at the DNS host would be:

```
Type:  CNAME
Name:  rapidpdf
Value: cname.vercel-dns.com
```
