// dockctl-agent: the LaunchAgent's main process.
//
// macOS grants Accessibility (needed to send keystrokes for `text` / `hotkey`
// actions) to the process launchd started, and its children inherit that. Running
// the Python daemon under this tiny binary means you grant "dockctl-agent" alone,
// not a Python interpreter every other script shares.
//
// Usage: dockctl-agent <program> [args...]   (spawns it, forwards signals, exits with its status)

#include <ApplicationServices/ApplicationServices.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 2;
    }
    if (!AXIsProcessTrusted()) {
        // Adds "dockctl-agent" to System Settings > Privacy & Security > Accessibility and asks the user
        // to switch it on. Only works here because launchd makes this process its own "responsible" process.
        const void *keys[] = {kAXTrustedCheckOptionPrompt};
        const void *vals[] = {kCFBooleanTrue};
        CFDictionaryRef opts = CFDictionaryCreate(NULL, keys, vals, 1, &kCFTypeDictionaryKeyCallBacks,
                                                  &kCFTypeDictionaryValueCallBacks);
        AXIsProcessTrustedWithOptions(opts);
        CFRelease(opts);
        fprintf(stderr, "dockctl-agent: no Accessibility permission yet; text/hotkey actions will fail "
                        "until it is switched on (dockctl permissions)\n");
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    if (posix_spawn(&child, argv[1], NULL, NULL, &argv[1], environ) != 0) {
        perror("dockctl-agent: spawn");
        return 127;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
