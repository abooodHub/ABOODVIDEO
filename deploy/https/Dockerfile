FROM golang:1.27.2-alpine AS builder
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN go install github.com/caddyserver/xcaddy/cmd/xcaddy@v0.4.7
RUN xcaddy build v2.11.7 --output /caddy \
    --replace golang.org/x/net=golang.org/x/net@v0.60.0

FROM caddy:2-alpine
RUN apk upgrade --no-cache
COPY --from=builder /caddy /usr/bin/caddy
