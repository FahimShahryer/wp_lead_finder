FROM node:20-alpine

WORKDIR /app

# Install deps separately so node_modules layer caches
COPY package.json package-lock.json* ./
RUN npm install

# Source (bind-mounted in compose dev mode)
COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
