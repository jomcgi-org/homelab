package main

import "fmt"

func add(a int, b int) int {
	return a + b
}

func main() {
	fmt.Println("hello", add(40, 2))
}
