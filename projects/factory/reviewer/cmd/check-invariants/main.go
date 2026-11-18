package main

import (
	"log"
	"os"

	"github.com/jomcgi/homelab/projects/factory/reviewer/review"
)

func main() {
	for _, file := range os.Args[1:] {
		data, err := os.ReadFile(file)
		if err != nil {
			log.Fatal(err)
		}
		if err := review.ParseMechanisms(file, data, map[string]bool{}); err != nil {
			log.Fatal(err)
		}
	}
}
