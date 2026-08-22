location ^~ /.well-known/acme-challenge/ {
    root /var/www/binhu-acme;
    default_type text/plain;
    try_files $uri =404;
}
