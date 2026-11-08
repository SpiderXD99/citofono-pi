/* relay - inoltro TCP minimale: ascolta su <porta> e per ogni connessione
 * apre una connessione a <dst_ip>:<dst_porta> e copia i byte nei due versi.
 * Un figlio per connessione. Nessuna dipendenza esterna (compilare statico).
 *
 *   relay <porta_locale> <dst_ip> <dst_porta>
 *
 * Esempio sul monitor del videocitofono:
 *   relay 8070 192.168.68.70 80   -> espone la web della placca su :8070
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

static void pump(int a, int b)
{
    char buf[16384];
    int mx = (a > b ? a : b) + 1;
    for (;;) {
        fd_set fs;
        FD_ZERO(&fs);
        FD_SET(a, &fs);
        FD_SET(b, &fs);
        if (select(mx, &fs, 0, 0, 0) < 0) {
            if (errno == EINTR) continue;
            return;
        }
        if (FD_ISSET(a, &fs)) {
            int n = read(a, buf, sizeof buf);
            if (n <= 0) return;
            for (int off = 0; off < n; ) {
                int w = write(b, buf + off, n - off);
                if (w <= 0) return;
                off += w;
            }
        }
        if (FD_ISSET(b, &fs)) {
            int n = read(b, buf, sizeof buf);
            if (n <= 0) return;
            for (int off = 0; off < n; ) {
                int w = write(a, buf + off, n - off);
                if (w <= 0) return;
                off += w;
            }
        }
    }
}

int main(int argc, char **argv)
{
    if (argc != 4) {
        fprintf(stderr, "uso: %s <porta> <dst_ip> <dst_porta>\n", argv[0]);
        return 1;
    }
    int lport = atoi(argv[1]);
    const char *dip = argv[2];
    int dport = atoi(argv[3]);

    signal(SIGCHLD, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);

    int ls = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = INADDR_ANY;
    sa.sin_port = htons(lport);
    if (bind(ls, (struct sockaddr *)&sa, sizeof sa) < 0) {
        perror("bind");
        return 1;
    }
    if (listen(ls, 16) < 0) {
        perror("listen");
        return 1;
    }
    fprintf(stderr, "relay: :%d -> %s:%d\n", lport, dip, dport);

    for (;;) {
        int cs = accept(ls, 0, 0);
        if (cs < 0) {
            if (errno == EINTR) continue;
            continue;
        }
        pid_t pid = fork();
        if (pid == 0) {
            close(ls);
            int ds = socket(AF_INET, SOCK_STREAM, 0);
            struct sockaddr_in da;
            memset(&da, 0, sizeof da);
            da.sin_family = AF_INET;
            da.sin_port = htons(dport);
            da.sin_addr.s_addr = inet_addr(dip);
            int one2 = 1;
            setsockopt(cs, IPPROTO_TCP, TCP_NODELAY, &one2, sizeof one2);
            if (connect(ds, (struct sockaddr *)&da, sizeof da) == 0) {
                setsockopt(ds, IPPROTO_TCP, TCP_NODELAY, &one2, sizeof one2);
                pump(cs, ds);
            }
            close(cs);
            close(ds);
            _exit(0);
        }
        close(cs);
    }
}
