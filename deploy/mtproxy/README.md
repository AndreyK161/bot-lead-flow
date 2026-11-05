# PravBuro Telegram MTProxy

The official Telegram MTProxy container is deployed separately from Lead Flow
under `/opt/pravburo/mtproxy`.

- Public endpoint: `proxy.prav-buro.ru:8444`
- Container endpoint: `443/tcp`
- Persistent secret: Docker volume `mtproxy_mtproxy-data`
- Restart policy: `unless-stopped`

The public port is intentionally not `443` because nginx already serves the
PravBuro web applications there. To print the generated connection links, run:

```sh
docker logs pravburo-mtproxy 2>&1 | grep -E 'Secret 1|t\.me link'
```

The link printed by the upstream image uses container port `443`; replace its
server with `proxy.prav-buro.ru` and its port with `8444`.
