ARG BUILD_FROM
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

RUN CGO_ENABLED=1 go build -trimpath -ldflags "-s -w" -o /out/llm_proxy .

FROM $BUILD_FROM

RUN apk add --no-cache \
    ca-certificates \
    libgcc \
    sqlite-libs

COPY --from=builder /out/llm_proxy /usr/bin/llm_proxy

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
