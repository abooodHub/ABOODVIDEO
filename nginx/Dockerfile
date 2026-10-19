FROM nginx:stable-alpine

RUN apk upgrade --no-cache

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/default.conf /etc/nginx/conf.d/default.conf
COPY templates/default.conf.template /etc/nginx/templates/default.conf.template
RUN mkdir -p /var/www/certbot/.well-known/acme-challenge && echo "acme-challenge-ok" > /var/www/certbot/.well-known/acme-challenge/test

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
