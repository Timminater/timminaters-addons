FROM golang:1.22-alpine AS builder

RUN apk add --no-cache \
    gcc \
    musl-dev \
    sqlite-dev

WORKDIR /build

COPY go.mod go.sum ./
RUN go mod download

COPY backend ./backend
COPY config ./config
COPY database ./database
COPY handlers ./handlers
COPY middleware ./middleware
COPY models ./models
COPY main.go ./

RUN mkdir -p /out \
    && CGO_ENABLED=1 CGO_CFLAGS="-D_LARGEFILE64_SOURCE -D_GNU_SOURCE" \
       go build -buildvcs=false -trimpath -ldflags "-s -w" -o /out/llm_proxy .

FROM ghcr.io/home-assistant/base@sha256:94ff231402a5e7ad2a82e261ad5fa4ffae7d7bb095c3febb2edbdf309c9b6aca

RUN apk add --no-cache \
    ca-certificates \
    libgcc \
    sqlite-libs

COPY --from=builder /out/llm_proxy /usr/bin/llm_proxy

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
