package com.acme.user;

import jakarta.persistence.*;

/** 사용자 엔티티 */
@Entity
@Table(name = "users")
public class User {
    @Id
    @GeneratedValue
    private Long id;

    private String name;

    private String email;

    public String getName() { return name; }
}
