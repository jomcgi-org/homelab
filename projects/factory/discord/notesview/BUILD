load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "notesview",
    srcs = [
        "render.go",
        "server.go",
        "vault.go",
    ],
    embedsrcs = [
        "static/graph.js",
        "static/notes.css",
        "templates/graph.html",
        "templates/home.html",
        "templates/layout.html",
        "templates/note.html",
        "templates/search.html",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/notesview",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/discord/artifacts",
        "//projects/factory/discord/notes",
        "@com_github_yuin_goldmark//:goldmark",
        "@com_github_yuin_goldmark//ast",
        "@com_github_yuin_goldmark//extension",
        "@com_github_yuin_goldmark//parser",
        "@com_github_yuin_goldmark//renderer",
        "@com_github_yuin_goldmark//renderer/html",
        "@com_github_yuin_goldmark//text",
        "@com_github_yuin_goldmark//util",
    ],
)

go_test(
    name = "notesview_test",
    srcs = ["notesview_test.go"],
    embed = [":notesview"],
    deps = [
        "//projects/factory/discord/artifacts",
        "@org_golang_x_net//html",
    ],
)
