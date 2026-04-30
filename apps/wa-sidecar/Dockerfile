FROM node:20-alpine
WORKDIR /app

# git: a Baileys peer dep (libsignal) is fetched via git+ssh during install.
# python3/make/g++: required by sharp / native modules built from source.
RUN apk add --no-cache git python3 make g++

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY src ./src

# Session creds + auth files persist here; volume-mounted from compose.
RUN mkdir -p /data/sessions
ENV SESSIONS_DIR=/data/sessions
ENV PORT=3001

EXPOSE 3001
CMD ["npm", "start"]
