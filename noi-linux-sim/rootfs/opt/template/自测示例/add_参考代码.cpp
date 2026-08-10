#include <cstdio>

int main() {
    freopen("add.in", "r", stdin);
    freopen("add.out", "w", stdout);
    long long a, b;
    scanf("%lld %lld", &a, &b);
    printf("%lld\n", a + b);
    fclose(stdin);
    fclose(stdout);
    return 0;
}

