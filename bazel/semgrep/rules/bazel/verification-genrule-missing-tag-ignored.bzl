native.genrule(
    name = "macro_test",
    outs = ["macro_test.txt"],
    cmd = "touch $@",
)
