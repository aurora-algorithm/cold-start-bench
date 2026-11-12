package bench;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

@RestController
class OrderController {
    static final long MAX_ID = 100_000;

    @GetMapping("/orders/{id}")
    ResponseEntity<Model.Order> order(@PathVariable long id) {
        if (id < 1 || id > MAX_ID) {
            return ResponseEntity.notFound().build();
        }
        return ResponseEntity.ok(OrderFormula.build(id));
    }
}
